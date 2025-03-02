# Copyright (c) OpenMMLab. All rights reserved.
import warnings
from typing import Dict, Tuple, Union

import torch
import torch.nn as nn
from torch import Tensor

from mmdet.registry import MODELS
from mmdet.structures import OptSampleList, SampleList
from ..layers import SinePositionalEncoding
from ..layers.transformer.grounding_dino_layers import (
    GroundingDinoTransformerDecoder, GroundingDinoTransformerEncoder)
from .dino import DINO
from .glip import (create_positive_map, create_positive_map_label_to_token,
                   run_ner)


@MODELS.register_module()
class GroundingDINOPropV2(DINO):
    """Implementation of `Grounding DINO: Marrying DINO with Grounded Pre-
    Training for Open-Set Object Detection.

    <https://arxiv.org/abs/2303.05499>`_

    Code is modified from the `official github repo
    <https://github.com/IDEA-Research/GroundingDINO>`_.
    """

    def __init__(self, language_model, *args, **kwargs) -> None:

        self.language_model_cfg = language_model
        self._special_tokens = '. '
        self.num_prop = kwargs['num_prop']
        super().__init__(*args, **kwargs)

    def _init_layers(self) -> None:
        """Initialize layers except for backbone, neck and bbox_head."""
        self.positional_encoding = SinePositionalEncoding(
            **self.positional_encoding)
        self.encoder = GroundingDinoTransformerEncoder(**self.encoder)
        self.decoder = GroundingDinoTransformerDecoder(**self.decoder)
        self.embed_dims = self.encoder.embed_dims
        self.query_embedding = nn.Embedding(self.num_queries, self.embed_dims)
        num_feats = self.positional_encoding.num_feats
        assert num_feats * 2 == self.embed_dims, \
            f'embed_dims should be exactly 2 times of num_feats. ' \
            f'Found {self.embed_dims} and {num_feats}.'

        self.level_embed = nn.Parameter(
            torch.Tensor(self.num_feature_levels, self.embed_dims))
        self.memory_trans_fc = nn.Linear(self.embed_dims, self.embed_dims)
        self.memory_trans_norm = nn.LayerNorm(self.embed_dims)

        # text modules
        self.language_model = MODELS.build(self.language_model_cfg)
        self.text_feat_map = nn.Linear(
            self.language_model.language_backbone.body.language_dim,
            self.embed_dims,
            bias=True)
        
        ## TODO: add prop. module
        self.prev_frame_q = {}


    def init_weights(self) -> None:
        """Initialize weights for Transformer and other components."""
        super().init_weights()
        nn.init.constant_(self.text_feat_map.bias.data, 0)
        nn.init.xavier_uniform_(self.text_feat_map.weight.data)

    def get_tokens_and_prompts(
            self,
            original_caption: Union[str, list, tuple],
            custom_entities: bool = False) -> Tuple[dict, str, list]:
        """Get the tokens positive and prompts for the caption."""
        if isinstance(original_caption, (list, tuple)) or custom_entities:
            if custom_entities and isinstance(original_caption, str):
                original_caption = original_caption.strip(self._special_tokens)
                original_caption = original_caption.split(self._special_tokens)
                original_caption = list(
                    filter(lambda x: len(x) > 0, original_caption))

            caption_string = ''
            tokens_positive = []
            for idx, word in enumerate(original_caption):
                tokens_positive.append(
                    [[len(caption_string),
                      len(caption_string) + len(word)]])
                caption_string += word
                caption_string += self._special_tokens
            # NOTE: Tokenizer in Grounding DINO is different from
            # that in GLIP. The tokenizer in GLIP will pad the
            # caption_string to max_length, while the tokenizer
            # in Grounding DINO will not.
            tokenized = self.language_model.tokenizer(
                [caption_string],
                padding='max_length'
                if self.language_model.pad_to_max else 'longest',
                return_tensors='pt')
            entities = original_caption
        else:
            if not original_caption.endswith('.'):
                original_caption = original_caption + self._special_tokens
            # NOTE: Tokenizer in Grounding DINO is different from
            # that in GLIP. The tokenizer in GLIP will pad the
            # caption_string to max_length, while the tokenizer
            # in Grounding DINO will not.
            tokenized = self.language_model.tokenizer(
                [original_caption],
                padding='max_length'
                if self.language_model.pad_to_max else 'longest',
                return_tensors='pt')
            tokens_positive, noun_phrases = run_ner(original_caption)
            entities = noun_phrases
            caption_string = original_caption

        return tokenized, caption_string, tokens_positive, entities

    def get_positive_map(self, tokenized, tokens_positive):
        positive_map = create_positive_map(tokenized, tokens_positive)
        positive_map_label_to_token = create_positive_map_label_to_token(
            positive_map, plus=1)
        return positive_map_label_to_token, positive_map

    def get_tokens_positive_and_prompts(
            self,
            original_caption: Union[str, list, tuple],
            custom_entities: bool = False) -> Tuple[dict, str, Tensor, list]:
        """Get the tokens positive and prompts for the caption.

        Args:
            original_caption (str): The original caption, e.g. 'bench . car .'
            custom_entities (bool, optional): Whether to use custom entities.
                If ``True``, the ``original_caption`` should be a list of
                strings, each of which is a word. Defaults to False.

        Returns:
            Tuple[dict, str, dict, str]: The dict is a mapping from each entity
            id, which is numbered from 1, to its positive token id.
            The str represents the prompts.
        """
        tokenized, caption_string, tokens_positive, entities = \
            self.get_tokens_and_prompts(
                original_caption, custom_entities)
        positive_map_label_to_token, positive_map = self.get_positive_map(
            tokenized, tokens_positive)
        return positive_map_label_to_token, caption_string, \
            positive_map, entities

    def forward_transformer(
        self,
        img_feats: Tuple[Tensor],
        text_dict: Dict,
        batch_data_samples: OptSampleList = None,
    ) -> Dict:
        encoder_inputs_dict, decoder_inputs_dict = self.pre_transformer(
            img_feats, batch_data_samples)

        encoder_outputs_dict = self.forward_encoder(
            **encoder_inputs_dict, text_dict=text_dict)

        tmp_dec_in, head_inputs_dict = self.pre_decoder(
            **encoder_outputs_dict, batch_data_samples=batch_data_samples)
        decoder_inputs_dict.update(tmp_dec_in)

        decoder_outputs_dict = self.forward_decoder(**decoder_inputs_dict)
        head_inputs_dict.update(decoder_outputs_dict)
        return head_inputs_dict

    def forward_encoder(self, feat: Tensor, feat_mask: Tensor,
                        feat_pos: Tensor, spatial_shapes: Tensor,
                        level_start_index: Tensor, valid_ratios: Tensor,
                        text_dict: Dict) -> Dict:
        text_token_mask = text_dict['text_token_mask']
        memory, memory_text = self.encoder(
            query=feat,
            query_pos=feat_pos,
            key_padding_mask=feat_mask,  # for self_attn
            spatial_shapes=spatial_shapes,
            level_start_index=level_start_index,
            valid_ratios=valid_ratios,
            # for text encoder
            memory_text=text_dict['embedded'],
            text_attention_mask=~text_token_mask,
            position_ids=text_dict['position_ids'],
            text_self_attention_masks=text_dict['masks'])
        encoder_outputs_dict = dict(
            memory=memory, # torch.Size([5, 19160, 256])
            memory_mask=feat_mask, # None
            spatial_shapes=spatial_shapes, # 90*160+45*80+23*40+12*20
            memory_text=memory_text, # torch.Size([5, 18, 256])
            text_token_mask=text_token_mask) # torch.Size([5, 18])
        return encoder_outputs_dict

    def pre_decoder(
        self,
        memory: Tensor,
        memory_mask: Tensor,
        spatial_shapes: Tensor,
        memory_text: Tensor,
        text_token_mask: Tensor,
        batch_data_samples: OptSampleList = None,
    ) -> Tuple[Dict]:
        bs, _, c = memory.shape

        output_memory, output_proposals = self.gen_encoder_output_proposals(
            memory, memory_mask, spatial_shapes)

        enc_outputs_class = self.bbox_head.cls_branches[
            self.decoder.num_layers](output_memory, memory_text,
                                     text_token_mask)
        cls_out_features = self.bbox_head.cls_branches[
            self.decoder.num_layers].max_text_len
        enc_outputs_coord_unact = self.bbox_head.reg_branches[
            self.decoder.num_layers](output_memory) + output_proposals

        # NOTE The DINO selects top-k proposals according to scores of
        # multi-class classification, while DeformDETR, where the input
        # is `enc_outputs_class[..., 0]` selects according to scores of
        # binary classification.
        ## possible options: 10, 50, 100, 200, 500, 800
        if self.training:
            topk_indices_list = []
            topk_score_list = []
            topk_coords_unact_list = []
            topk_coords_list = []
            query_list = []
            for i in range(bs):
                # perform prop.
                if i == 0: # first frame
                    topk_indices = torch.topk(
                    enc_outputs_class[i:i + 1].max(-1)[0], k=self.num_queries, dim=1)[1]
                    # top_k_score: ([5, 900, 256]), 
                    # 5: num_frames, 900: num_queries, 256: cls_out_features
                    topk_score = torch.gather(
                        enc_outputs_class[i:i + 1], 1,
                        topk_indices.unsqueeze(-1).repeat(1, 1, cls_out_features))
                    # topk_coords_unact: torch.Size([5, 900, 4]), 
                    # 5:num_frames, 900: num_queries, 4: bbox_head.reg_branches[-1].out_channels
                    topk_coords_unact = torch.gather(
                        enc_outputs_coord_unact[i:i + 1], 1,
                        topk_indices.unsqueeze(-1).repeat(1, 1, 4))
                    topk_coords = topk_coords_unact.sigmoid()
                    topk_coords_unact = topk_coords_unact.detach()

                    query = self.query_embedding.weight[:, None, :]
                    query = query.repeat(1, bs, 1).transpose(0, 1)
                    query = query[i:i+1]

                    ## TODO: 900 -> 100 and save
                    # detach first
                    enc_outputs_class_for_prop = enc_outputs_class[i:i + 1].detach()
                    enc_outputs_coord_unact_for_prop = enc_outputs_coord_unact[i:i + 1].detach()
                    # select dim=900 features
                    enc_outputs_class_for_prop = enc_outputs_class_for_prop[0, topk_indices[0]].unsqueeze(0)
                    enc_outputs_coord_unact_for_prop = enc_outputs_coord_unact_for_prop[0, topk_indices[0]].unsqueeze(0)

                    topk_indices_prop = torch.topk(enc_outputs_class_for_prop.max(-1)[0], k=self.num_prop, dim=1)[1]
                    topk_score_prop = torch.gather(
                        enc_outputs_class_for_prop, 1,
                        topk_indices_prop.unsqueeze(-1).repeat(1, 1, cls_out_features))
                    # topk_coords_unact: torch.Size([5, 900, 4]), 
                    # 5:num_frames, 900: num_queries, 4: bbox_head.reg_branches[-1].out_channels
                    topk_coords_unact_prop = torch.gather(
                        enc_outputs_coord_unact_for_prop, 1,
                        topk_indices_prop.unsqueeze(-1).repeat(1, 1, 4))
                    topk_coords_prop = topk_coords_unact_prop.sigmoid()
                    topk_coords_unact_prop = topk_coords_unact_prop.detach()
                    # select from here => 900 to 100
                    query_for_prop = query[0, topk_indices_prop[0]].detach().unsqueeze(0)
                    
                    # DONE: enc_outputs_class_for_prop select 100 and prop. also
                    enc_outputs_class_for_prop_next = enc_outputs_class_for_prop[0, topk_indices_prop[0]].unsqueeze(0)
                    enc_outputs_coord_unact_for_prop_next = enc_outputs_coord_unact_for_prop[0, topk_indices_prop[0]].unsqueeze(0)
                else: # other frames
                    # perform propagation
                    # select 900 features
                    topk_indices = torch.topk(
                    enc_outputs_class[i:i + 1].max(-1)[0], k=self.num_queries, dim=1)[1]
                    # top_k_score: ([5, 900, 256]), 
                    # 5: num_frames, 900: num_queries, 256: cls_out_features
                    topk_score = torch.gather(
                        enc_outputs_class[i:i + 1], 1,
                        topk_indices.unsqueeze(-1).repeat(1, 1, cls_out_features))
                    # topk_coords_unact: torch.Size([5, 900, 4]), 
                    # 5:num_frames, 900: num_queries, 4: bbox_head.reg_branches[-1].out_channels
                    topk_coords_unact = torch.gather(
                        enc_outputs_coord_unact[i:i + 1], 1,
                        topk_indices.unsqueeze(-1).repeat(1, 1, 4))
                    topk_coords = topk_coords_unact.sigmoid()
                    topk_coords_unact = topk_coords_unact.detach()

                    query = self.query_embedding.weight[:, None, :]
                    query = query.repeat(1, bs, 1).transpose(0, 1)
                    query = query[i:i+1]

                    ## build up current frame features
                    enc_outputs_class_curr_frame = enc_outputs_class[i:i + 1].detach()
                    enc_outputs_coord_unact_curr_frame = enc_outputs_coord_unact[i:i + 1].detach()

                    # NOTE: build up top k indices for selecting query
                    #---- these two feature is 900 dim
                    enc_outputs_class_curr_frame = enc_outputs_class_curr_frame[0, topk_indices[0]].unsqueeze(0)
                    enc_outputs_coord_unact_curr_frame = enc_outputs_coord_unact_curr_frame[0, topk_indices[0]].unsqueeze(0)
                    #---- these two feature is 900 dim

                    #---- NOTE: select 900 - num_prop features to be used here
                    topk_indices_curr_frame = torch.topk(enc_outputs_coord_unact_curr_frame.max(-1)[0], k=self.num_queries - self.num_prop, dim=1)[1]
                    topk_score_curr_frame = torch.gather(
                        enc_outputs_class_curr_frame, 1,
                        topk_indices_curr_frame.unsqueeze(-1).repeat(1, 1, cls_out_features))
                    topk_coords_unact_curr_frame = torch.gather(
                        enc_outputs_coord_unact_curr_frame, 1,
                        topk_indices_curr_frame.unsqueeze(-1).repeat(1, 1, 4))
                    topk_coords_curr_frame = topk_coords_unact_curr_frame.sigmoid()
                    topk_coords_unact_curr_frame = topk_coords_unact_curr_frame.detach()
                    query_curr_frame = query[0, topk_indices_curr_frame[0]].detach().unsqueeze(0) # select 900 - num_prop features
                    #---- NOTE: select 900 - num_prop features to be used here

                    ## DONE: enc_outputs_class_curr_frame select 900 - prop for use
                    enc_outputs_class_curr_frame_query_prop = enc_outputs_class_curr_frame[0, topk_indices_curr_frame[0]].unsqueeze(0)
                    enc_outputs_coord_unact_curr_frame_query_prop = enc_outputs_coord_unact_curr_frame[0, topk_indices_curr_frame[0]].unsqueeze(0)

                    ## TODO: concat 100, 800
                    # take out prev frame features
                    prev_topk_indices = self.prev_frame_q['topk_indices']
                    prev_topk_score = self.prev_frame_q['topk_score']
                    prev_topk_coords_unact = self.prev_frame_q['topk_coords_unact']
                    prev_topk_coords = self.prev_frame_q['topk_coords']
                    prev_query = self.prev_frame_q['query']
                    prev_enc_outputs_class = self.prev_frame_q['enc_outputs_class']
                    prev_enc_outputs_coord_unact = self.prev_frame_q['enc_outputs_coord_unact'] 

                    # concat with curr frame feature and overwrite
                    topk_indices = torch.cat((topk_indices_curr_frame, prev_topk_indices), dim=1)
                    topk_score = torch.cat((topk_score_curr_frame, prev_topk_score), dim=1)
                    topk_coords_unact = torch.cat((topk_coords_unact_curr_frame, prev_topk_coords_unact), dim=1)
                    topk_coords = torch.cat((topk_coords_curr_frame, prev_topk_coords), dim=1)
                    query = torch.cat((query_curr_frame, prev_query), dim=1)

                    ### for prop.
                    # TODO: enc outputs: 900 => select 100
                    enc_outputs_class_curr_frame_fuse_last = torch.cat((enc_outputs_class_curr_frame_query_prop, prev_enc_outputs_class), dim=1)
                    enc_outputs_coord_unact_curr_frame_fuse_last = torch.cat((enc_outputs_coord_unact_curr_frame_query_prop, prev_enc_outputs_coord_unact), dim=1)

                    ## NOTE: 900 -> 100 and save for the next frame (using v2)
                    topk_indices_prop = torch.topk(enc_outputs_class_curr_frame_fuse_last.max(-1)[0], k=self.num_prop, dim=1)[1]
                    topk_score_prop = torch.gather(
                        enc_outputs_class_curr_frame_fuse_last, 1,
                        topk_indices_prop.unsqueeze(-1).repeat(1, 1, cls_out_features))
                    topk_coords_unact_prop = torch.gather(
                        enc_outputs_coord_unact_curr_frame_fuse_last, 1,
                        topk_indices_prop.unsqueeze(-1).repeat(1, 1, 4))
                    topk_coords_prop = topk_coords_unact_prop.sigmoid()
                    topk_coords_unact_prop = topk_coords_unact_prop.detach()
                    query_for_prop = query[0, topk_indices_prop[0]].detach().unsqueeze(0) # select from here => 900 to 100

                    # DONE: enc_outputs_class_for_prop select 100 and prop. also
                    enc_outputs_class_for_prop_next = enc_outputs_class_curr_frame_fuse_last[0, topk_indices_prop[0]].unsqueeze(0)
                    enc_outputs_coord_unact_for_prop_next = enc_outputs_coord_unact_curr_frame_fuse_last[0, topk_indices_prop[0]].unsqueeze(0)


                # collect all features
                topk_indices_list.append(topk_indices)
                topk_score_list.append(topk_score)
                topk_coords_unact_list.append(topk_coords_unact)
                topk_coords_list.append(topk_coords)
                query_list.append(query)
                # save for the next frame
                self.prev_frame_q['topk_indices'] = topk_indices_prop
                self.prev_frame_q['topk_score'] = topk_score_prop
                self.prev_frame_q['topk_coords_unact'] = topk_coords_unact_prop
                self.prev_frame_q['topk_coords'] = topk_coords_prop
                self.prev_frame_q['query'] = query_for_prop

                self.prev_frame_q['enc_outputs_class'] = enc_outputs_class_for_prop_next
                self.prev_frame_q['enc_outputs_coord_unact'] = enc_outputs_coord_unact_for_prop_next
                
            # concat all features for use.
            topk_indices = torch.cat(topk_indices_list, dim=0)
            topk_score = torch.cat(topk_score_list, dim=0)
            topk_coords_unact = torch.cat(topk_coords_unact_list, dim=0)
            topk_coords = torch.cat(topk_coords_list, dim=0)
            query = torch.cat(query_list, dim=0)
        else:
            # inference
            curr_frame_idx = batch_data_samples[0].frame_idx
            if curr_frame_idx == 0:
                # first frame
                topk_indices = torch.topk(
                enc_outputs_class.max(-1)[0], k=self.num_queries, dim=1)[1]
                # top_k_score: ([5, 900, 256]), 
                # 5: num_frames, 900: num_queries, 256: cls_out_features
                topk_score = torch.gather(
                    enc_outputs_class, 1,
                    topk_indices.unsqueeze(-1).repeat(1, 1, cls_out_features))
                # topk_coords_unact: torch.Size([5, 900, 4]), 
                # 5:num_frames, 900: num_queries, 4: bbox_head.reg_branches[-1].out_channels
                topk_coords_unact = torch.gather(
                    enc_outputs_coord_unact, 1,
                    topk_indices.unsqueeze(-1).repeat(1, 1, 4))
                topk_coords = topk_coords_unact.sigmoid()
                topk_coords_unact = topk_coords_unact.detach()

                query = self.query_embedding.weight[:, None, :]
                query = query.repeat(1, bs, 1).transpose(0, 1)

                # ## TODO: 900 -> 100 and save
                # detach first
                enc_outputs_class_for_prop = enc_outputs_class.detach()
                enc_outputs_coord_unact_for_prop = enc_outputs_coord_unact.detach()
                # select dim=900 features
                enc_outputs_class_for_prop = enc_outputs_class_for_prop[0, topk_indices[0]].unsqueeze(0)
                enc_outputs_coord_unact_for_prop = enc_outputs_coord_unact_for_prop[0, topk_indices[0]].unsqueeze(0)

                topk_indices_prop = torch.topk(enc_outputs_class_for_prop.max(-1)[0], k=self.num_prop, dim=1)[1]
                topk_score_prop = torch.gather(
                    enc_outputs_class_for_prop, 1,
                    topk_indices_prop.unsqueeze(-1).repeat(1, 1, cls_out_features))
                # topk_coords_unact: torch.Size([5, 900, 4]), 
                # 5:num_frames, 900: num_queries, 4: bbox_head.reg_branches[-1].out_channels
                topk_coords_unact_prop = torch.gather(
                    enc_outputs_coord_unact_for_prop, 1,
                    topk_indices_prop.unsqueeze(-1).repeat(1, 1, 4))
                topk_coords_prop = topk_coords_unact_prop.sigmoid()
                topk_coords_unact_prop = topk_coords_unact_prop.detach()
                # select from here => 900 to 100
                query_for_prop = query[0, topk_indices_prop[0]].detach().unsqueeze(0)
                # DONE: enc_outputs_class_for_prop select 100 and prop. also
                enc_outputs_class_for_prop_next = enc_outputs_class_for_prop[0, topk_indices_prop[0]].unsqueeze(0)
                enc_outputs_coord_unact_for_prop_next = enc_outputs_coord_unact_for_prop[0, topk_indices_prop[0]].unsqueeze(0)

                # enc_outputs_class_for_prop_next = enc_outputs_class
                # enc_outputs_coord_unact_for_prop_next = enc_outputs_coord_unact

            else:
                # perform propagation
                # select 900 features

                topk_indices = torch.topk(
                enc_outputs_class.max(-1)[0], k=self.num_queries, dim=1)[1]
                # top_k_score: ([5, 900, 256]), 
                # 5: num_frames, 900: num_queries, 256: cls_out_features
                topk_score = torch.gather(
                    enc_outputs_class, 1,
                    topk_indices.unsqueeze(-1).repeat(1, 1, cls_out_features))
                # topk_coords_unact: torch.Size([5, 900, 4]), 
                # 5:num_frames, 900: num_queries, 4: bbox_head.reg_branches[-1].out_channels
                topk_coords_unact = torch.gather(
                    enc_outputs_coord_unact, 1,
                    topk_indices.unsqueeze(-1).repeat(1, 1, 4))
                topk_coords = topk_coords_unact.sigmoid()
                topk_coords_unact = topk_coords_unact.detach()

                query = self.query_embedding.weight[:, None, :]
                query = query.repeat(1, bs, 1).transpose(0, 1)

                ## build up current frame features
                enc_outputs_class_curr_frame = enc_outputs_class.detach()
                enc_outputs_coord_unact_curr_frame = enc_outputs_coord_unact.detach()
                # NOTE: build up top k indices for selecting query
                # these two feature is 900 dim
                enc_outputs_class_curr_frame = enc_outputs_class_curr_frame[0, topk_indices[0]].unsqueeze(0)
                enc_outputs_coord_unact_curr_frame = enc_outputs_coord_unact_curr_frame[0, topk_indices[0]].unsqueeze(0)

                # NOTE: select 900 - num_prop features to be used here
                topk_indices_curr_frame = torch.topk(enc_outputs_coord_unact_curr_frame.max(-1)[0], k=self.num_queries - self.num_prop, dim=1)[1]
                topk_score_curr_frame = torch.gather(
                    enc_outputs_class_curr_frame, 1,
                    topk_indices_curr_frame.unsqueeze(-1).repeat(1, 1, cls_out_features))
                topk_coords_unact_curr_frame = torch.gather(
                    enc_outputs_coord_unact_curr_frame, 1,
                    topk_indices_curr_frame.unsqueeze(-1).repeat(1, 1, 4))
                topk_coords_curr_frame = topk_coords_unact_curr_frame.sigmoid()
                topk_coords_unact_curr_frame = topk_coords_unact_curr_frame.detach()
                query_curr_frame = query[0, topk_indices_curr_frame[0]].detach().unsqueeze(0) # select 900 - num_prop features

                ## DONE: enc_outputs_class_curr_frame select 900 - prop for use
                enc_outputs_class_curr_frame_query_prop = enc_outputs_class_curr_frame[0, topk_indices_curr_frame[0]].unsqueeze(0)
                enc_outputs_coord_unact_curr_frame_query_prop = enc_outputs_coord_unact_curr_frame[0, topk_indices_curr_frame[0]].unsqueeze(0)
                # take out prev frame features
                prev_topk_indices = self.prev_frame_q['topk_indices']
                prev_topk_score = self.prev_frame_q['topk_score']
                prev_topk_coords_unact = self.prev_frame_q['topk_coords_unact']
                prev_topk_coords = self.prev_frame_q['topk_coords']
                prev_query = self.prev_frame_q['query']
                prev_enc_outputs_class = self.prev_frame_q['enc_outputs_class']
                prev_enc_outputs_coord_unact = self.prev_frame_q['enc_outputs_coord_unact'] 
                # concat with curr frame feature and overwrite
                # topk_indices = torch.cat((prev_topk_indices, topk_indices_curr_frame), dim=1)
                # topk_score = torch.cat((prev_topk_score, topk_score_curr_frame), dim=1)
                # topk_coords_unact = torch.cat((prev_topk_coords_unact, topk_coords_unact_curr_frame), dim=1)
                # topk_coords = torch.cat((prev_topk_coords, topk_coords_curr_frame), dim=1)
                # query = torch.cat((prev_query, query_curr_frame), dim=1)
                topk_indices = torch.cat((topk_indices_curr_frame, prev_topk_indices), dim=1)
                topk_score = torch.cat((topk_score_curr_frame, prev_topk_score), dim=1)
                topk_coords_unact = torch.cat((topk_coords_unact_curr_frame, prev_topk_coords_unact), dim=1)
                topk_coords = torch.cat((topk_coords_curr_frame, prev_topk_coords), dim=1)
                query = torch.cat((query_curr_frame, prev_query), dim=1)

                ### for prop.
                # TODO: enc outputs: 900 => select 100
                enc_outputs_class_curr_frame_fuse_last = torch.cat((enc_outputs_class_curr_frame_query_prop, prev_enc_outputs_class), dim=1)
                enc_outputs_coord_unact_curr_frame_fuse_last = torch.cat((enc_outputs_coord_unact_curr_frame_query_prop, prev_enc_outputs_coord_unact), dim=1)


                ## NOTE: 900 -> 100 and save for the next frame
                topk_indices_prop = torch.topk(enc_outputs_class_curr_frame_fuse_last.max(-1)[0], k=self.num_prop, dim=1)[1]
                topk_score_prop = torch.gather(
                    enc_outputs_class_curr_frame_fuse_last, 1,
                    topk_indices_prop.unsqueeze(-1).repeat(1, 1, cls_out_features))
                topk_coords_unact_prop = torch.gather(
                    enc_outputs_coord_unact_curr_frame_fuse_last, 1,
                    topk_indices_prop.unsqueeze(-1).repeat(1, 1, 4))
                topk_coords_prop = topk_coords_unact_prop.sigmoid()
                topk_coords_unact_prop = topk_coords_unact_prop.detach()
                query_for_prop = query[0, topk_indices_prop[0]].detach().unsqueeze(0) # select from here => 900 to 100

                ### BUG: 
                # enc_outputs_class_tmp = torch.cat((enc_outputs_class, self.prev_frame_q['enc_outputs_class']), dim=1)
                # enc_outputs_coord_unact_tmp = torch.cat((enc_outputs_coord_unact, self.prev_frame_q['enc_outputs_coord_unact']), dim=1)
                # # select 900 features
                # topk_indices = torch.topk(
                # enc_outputs_class_tmp.max(-1)[0], k=self.num_queries, dim=1)[1]
                # # top_k_score: ([5, 900, 256]), 
                # # 5: num_frames, 900: num_queries, 256: cls_out_features
                # topk_score = torch.gather(
                #     enc_outputs_class_tmp, 1,
                #     topk_indices.unsqueeze(-1).repeat(1, 1, cls_out_features))
                # # topk_coords_unact: torch.Size([5, 900, 4]), 
                # # 5:num_frames, 900: num_queries, 4: bbox_head.reg_branches[-1].out_channels
                # topk_coords_unact = torch.gather(
                #     enc_outputs_coord_unact_tmp, 1,
                #     topk_indices.unsqueeze(-1).repeat(1, 1, 4))
                # topk_coords = topk_coords_unact.sigmoid()
                # topk_coords_unact = topk_coords_unact.detach()

                # query = self.query_embedding.weight[:, None, :]
                # query = query.repeat(1, bs, 1).transpose(0, 1)
                ### BUG:

                # DONE: enc_outputs_class_for_prop select 100 and prop. also
                enc_outputs_class_for_prop_next = enc_outputs_class_curr_frame_fuse_last[0, topk_indices_prop[0]].unsqueeze(0)
                enc_outputs_coord_unact_for_prop_next = enc_outputs_coord_unact_curr_frame_fuse_last[0, topk_indices_prop[0]].unsqueeze(0)

                # enc_outputs_class_for_prop_next = enc_outputs_class
                # enc_outputs_coord_unact_for_prop_next = enc_outputs_coord_unact

                
            # save for the next frame
            self.prev_frame_q['topk_indices'] = topk_indices_prop
            self.prev_frame_q['topk_score'] = topk_score_prop
            self.prev_frame_q['topk_coords_unact'] = topk_coords_unact_prop
            self.prev_frame_q['topk_coords'] = topk_coords_prop
            self.prev_frame_q['query'] = query_for_prop
            self.prev_frame_q['enc_outputs_class'] = enc_outputs_class_for_prop_next
            self.prev_frame_q['enc_outputs_coord_unact'] = enc_outputs_coord_unact_for_prop_next

        # TODO: view dn_query_generator as a black box
        if self.training:
            dn_label_query, dn_bbox_query, dn_mask, dn_meta = \
                self.dn_query_generator(batch_data_samples)
            
            query = torch.cat([dn_label_query, query], dim=1)
            reference_points = torch.cat([dn_bbox_query, topk_coords_unact],
                                         dim=1)
        else:
            reference_points = topk_coords_unact
            dn_mask, dn_meta = None, None
        reference_points = reference_points.sigmoid()

        decoder_inputs_dict = dict(
            query=query,
            memory=memory,
            reference_points=reference_points,
            dn_mask=dn_mask,
            memory_text=memory_text,
            text_attention_mask=~text_token_mask,
        )
        # NOTE DINO calculates encoder losses on scores and coordinates
        # of selected top-k encoder queries, while DeformDETR is of all
        # encoder queries.
        head_inputs_dict = dict(
            enc_outputs_class=topk_score,
            enc_outputs_coord=topk_coords,
            dn_meta=dn_meta) if self.training else dict()
        # append text_feats to head_inputs_dict
        head_inputs_dict['memory_text'] = memory_text
        head_inputs_dict['text_token_mask'] = text_token_mask
        return decoder_inputs_dict, head_inputs_dict

    def loss(self, batch_inputs: Tensor,
             batch_data_samples: SampleList) -> Union[dict, list]:
        # TODO: Only open vocabulary tasks are supported for training now.
        text_prompts = [
            data_samples.text for data_samples in batch_data_samples
        ]

        gt_labels = [
            data_samples.gt_instances.labels
            for data_samples in batch_data_samples
        ]
        new_text_prompts = []
        positive_maps = []
        if len(set(text_prompts)) == 1:
            # All the text prompts are the same,
            # so there is no need to calculate them multiple times.
            tokenized, caption_string, tokens_positive, _ = \
                self.get_tokens_and_prompts(
                    text_prompts[0], True)
            new_text_prompts = [caption_string] * len(batch_inputs)
            for gt_label in gt_labels:
                new_tokens_positive = [
                    tokens_positive[label] for label in gt_label
                ]
                _, positive_map = self.get_positive_map(
                    tokenized, new_tokens_positive)
                positive_maps.append(positive_map)
        else:
            for text_prompt, gt_label in zip(text_prompts, gt_labels):
                tokenized, caption_string, tokens_positive, _ = \
                    self.get_tokens_and_prompts(
                        text_prompt, True)
                new_tokens_positive = [
                    tokens_positive[label] for label in gt_label
                ]
                _, positive_map = self.get_positive_map(
                    tokenized, new_tokens_positive)
                positive_maps.append(positive_map)
                new_text_prompts.append(caption_string)

        text_dict = self.language_model(new_text_prompts)
        if self.text_feat_map is not None:
            text_dict['embedded'] = self.text_feat_map(text_dict['embedded'])

        for i, data_samples in enumerate(batch_data_samples):
            positive_map = positive_maps[i].to(
                batch_inputs.device).bool().float()
            text_token_mask = text_dict['text_token_mask'][i]
            data_samples.gt_instances.positive_maps = positive_map
            data_samples.gt_instances.text_token_mask = \
                text_token_mask.unsqueeze(0).repeat(
                    len(positive_map), 1)

        visual_features = self.extract_feat(batch_inputs)
        head_inputs_dict = self.forward_transformer(visual_features, text_dict,
                                                    batch_data_samples)

        losses = self.bbox_head.loss(
            **head_inputs_dict, batch_data_samples=batch_data_samples)
        return losses

    def predict(self, batch_inputs, batch_data_samples, rescale: bool = True):
        text_prompts = [
            data_samples.text for data_samples in batch_data_samples
        ]
        if 'custom_entities' in batch_data_samples[0]:
            # Assuming that the `custom_entities` flag
            # inside a batch is always the same. For single image inference
            custom_entities = batch_data_samples[0].custom_entities
        else:
            custom_entities = False
        if len(text_prompts) == 1:
            # All the text prompts are the same,
            # so there is no need to calculate them multiple times.
            _positive_maps_and_prompts = [
                self.get_tokens_positive_and_prompts(text_prompts[0],
                                                     custom_entities)
            ] * len(batch_inputs)
        else:
            _positive_maps_and_prompts = [
                self.get_tokens_positive_and_prompts(text_prompt,
                                                     custom_entities)
                for text_prompt in text_prompts
            ]
        token_positive_maps, text_prompts, _, entities = zip(
            *_positive_maps_and_prompts)
        # extract text feats
        text_dict = self.language_model(list(text_prompts))
        # text feature map layer
        if self.text_feat_map is not None:
            text_dict['embedded'] = self.text_feat_map(text_dict['embedded'])

        for i, data_samples in enumerate(batch_data_samples):
            data_samples.token_positive_map = token_positive_maps[i]

        # image feature extraction
        visual_feats = self.extract_feat(batch_inputs)

        head_inputs_dict = self.forward_transformer(visual_feats, text_dict,
                                                    batch_data_samples)
        results_list = self.bbox_head.predict(
            **head_inputs_dict,
            rescale=rescale,
            batch_data_samples=batch_data_samples)
        for data_sample, pred_instances, entity in zip(batch_data_samples,
                                                       results_list, entities):
            if len(pred_instances) > 0:
                label_names = []
                for labels in pred_instances.labels:
                    if labels >= len(entity):
                        warnings.warn(
                            'The unexpected output indicates an issue with '
                            'named entity recognition. You can try '
                            'setting custom_entities=True and running '
                            'again to see if it helps.')
                        label_names.append('unobject')
                    else:
                        label_names.append(entity[labels])
                # for visualization
                pred_instances.label_names = label_names
            data_sample.pred_instances = pred_instances
        return batch_data_samples
