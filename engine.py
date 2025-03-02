"""
Train and eval functions used in main.py
Modified from ReferFormer (https://github.com/wjn922/ReferFormer)
"""
import math
from models import postprocessors
import os
import sys
from typing import Iterable

import torch
import torch.distributed as dist

import util.misc as utils
from datasets.coco_eval import CocoEvaluator
from datasets.refexp_eval import RefExpEvaluator

from pycocotools.coco import COCO
from pycocotools.cocoeval import COCOeval
from datasets.a2d_eval import calculate_precision_at_k_and_iou_metrics

import torchvision
from segment_anything.utils.transforms import ResizeLongestSide

import mmcv
from torchvision.io import read_video
import cv2

from models.postprocessors import build_postprocessors

def train_one_epoch(model: torch.nn.Module, criterion: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, max_norm: float = 0, args=None, writer=None):
    model.train()
    criterion.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Exp: {}, Epoch: [{}]'.format(args.output_dir, epoch)
    print_freq = 10
    n_iters = 0
    for samples, targets in metric_logger.log_every(data_loader, print_freq, header):
        samples = samples.to(device)
        captions = [t["caption"] for t in targets]
        targets = utils.targets_to(targets, device) 

        if args.online:
            loss_dict = model(samples, captions, targets)
        elif args.semi_online:
            loss_dict = model(samples, captions, targets)
        else:
            outputs = model(samples, captions, targets)
            loss_dict, _ = criterion(outputs, targets)

        weight_dict = criterion.weight_dict
        losses = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys() if k in weight_dict)

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        loss_dict_reduced_unscaled = {f'{k}_unscaled': v
                                      for k, v in loss_dict_reduced.items()}
        loss_dict_reduced_scaled = {k: v * weight_dict[k]
                                    for k, v in loss_dict_reduced.items() if k in weight_dict}
        losses_reduced_scaled = sum(loss_dict_reduced_scaled.values())

        loss_value = losses_reduced_scaled.item()

        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            print(loss_dict_reduced)
            sys.exit(1)
        optimizer.zero_grad()
        losses.backward()
        if max_norm > 0:
            grad_total_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm)
        else:
            grad_total_norm = utils.get_total_grad_norm(model.parameters(), max_norm)
        optimizer.step()

        metric_logger.update(loss=loss_value, **loss_dict_reduced_scaled, **loss_dict_reduced_unscaled)
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])
        metric_logger.update(grad_norm=grad_total_norm)

        for k in loss_dict.keys():
            writer.add_scalar(str(k), loss_dict[k].cpu().detach().item(), len(data_loader)*epoch + n_iters)
        writer.add_scalar('lr', optimizer.param_groups[0]['lr'], len(data_loader)*epoch + n_iters)
        n_iters += 1

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}



def train_one_epoch_sam(model: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, max_norm: float = 0, args=None, writer=None):
    
    ## NOT DONE ## 
    ## TODO: train sam LORA here, need gt bboxes as the input to sam model
    # this info. should be in the targets already

    model.train()
    # criterion.train()

    ce_loss = torch.nn.CrossEntropyLoss()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Exp: {}, Epoch: [{}]'.format(args.output_dir, epoch)
    print_freq = 1000
    n_iters = 0
    for samples, targets in metric_logger.log_every(data_loader, print_freq, header):
        samples = samples.to(device)
        # captions = [t["caption"] for t in targets]
        targets = utils.targets_to(targets, device) 
        
        # transform boxes to sam model input
        H, W = targets[0]['size']
        H, W = H.item(), W.item()
        boxes_xyxy = torchvision.ops.box_convert(targets[0]['boxes'], in_fmt='cxcywh', out_fmt='xyxy') * torch.Tensor([W, H, W, H]).cuda()
        if args.distributed:
            trans = ResizeLongestSide(model.module.sam.image_encoder.img_size)
        else:
            trans = ResizeLongestSide(model.sam.image_encoder.img_size)
        transformed_boxes = trans.apply_boxes_torch(boxes_xyxy, targets[0]['size']).to(device)


        ## TODO: build batched_input for SAM
        input_dict = {'image': samples, 
                      'bbox': transformed_boxes,
                      'orig_size': targets[0]['orig_size'], # NOTE: not sure use orig_size or size
                      'size': targets[0]['size'], # NOTE: not sure use orig_size or size
                        #  'point_labels': None,
                        #  'point_coords': None,
                        #  'mask_inputs': None
                        }

        if args.online:
            outputs = model(input_dict, False)
        # elif args.semi_online:
        #     loss_dict = model(samples, captions, targets)
        # else:
        #     outputs = model(samples, captions, targets)
        #     loss_dict, _ = criterion(outputs, targets)
        
        # NOTE: this masks is the output of sam model, which is the logits, and it is "unthres."
        logits = outputs['masks']
        # loss = criterion(outputs, targets)
        loss = torchvision.ops.sigmoid_focal_loss(logits.squeeze(0),  targets[0]['masks'][:].float(), reduction='mean')
        # loss = ce_loss(logits.squeeze(0), targets[0]['masks'][:].float())
        
        # weight_dict = criterion.weight_dict
        # losses = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys() if k in weight_dict)

        # reduce losses over all GPUs for logging purposes
        # loss_dict_reduced = utils.reduce_dict(loss_dict)
        # loss_dict_reduced_unscaled = {f'{k}_unscaled': v
        #                               for k, v in loss_dict_reduced.items()}
        # loss_dict_reduced_scaled = {k: v * weight_dict[k]
        #                             for k, v in loss_dict_reduced.items() if k in weight_dict}
        # losses_reduced_scaled = sum(loss_dict_reduced_scaled.values())
        loss_dict_reduced_unscaled = {}
        loss_dict_reduced_scaled = {}
        # loss_value = losses_reduced_scaled.item()
        loss_value = loss.item()

        if not math.isfinite(loss):
            print("Loss is {}, stopping training".format(loss))
            # print(loss_dict_reduced)
            sys.exit(1)
        
        optimizer.zero_grad()
        loss.backward()
        if max_norm > 0:
            grad_total_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm)
        else:
            grad_total_norm = utils.get_total_grad_norm(model.parameters(), max_norm)
        optimizer.step()

        metric_logger.update(loss=loss_value, **loss_dict_reduced_scaled, **loss_dict_reduced_unscaled)
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])
        metric_logger.update(grad_norm=grad_total_norm)

        # for k in loss_dict.keys():
        #     writer.add_scalar(str(k), loss_dict[k].cpu().detach().item(), len(data_loader)*epoch + n_iters)
        writer.add_scalar('lr', optimizer.param_groups[0]['lr'], len(data_loader)*epoch + n_iters)
        n_iters += 1

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


def train_one_epoch_gdino(model: torch.nn.Module, criterion: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, max_norm: float = 0, args=None, writer=None):
    model.train()
    
    model._set_static_graph()

    criterion.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Exp: {}, Epoch: [{}]'.format(args.output_dir, epoch)
    print_freq = 1000
    n_iters = 0
    for samples, targets in metric_logger.log_every(data_loader, print_freq, header):
        if args.num_train_steps != -1:
            if n_iters > args.num_train_steps:
                break
        
        samples = samples.to(device)
        # captions = [t["caption"] for t in targets]
        targets_loss = utils.targets_to(targets, device) 
        # if args.online:
        #     loss_dict = model(samples, captions)
        # elif args.semi_online:
        #     loss_dict = model(samples, captions)
        # else:
        outputs = model(samples.tensors, targets)
        loss_dict, _ = criterion(outputs, targets_loss)

        weight_dict = criterion.weight_dict
        losses = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys() if k in weight_dict)

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        loss_dict_reduced_unscaled = {f'{k}_unscaled': v
                                      for k, v in loss_dict_reduced.items()}
        loss_dict_reduced_scaled = {k: v * weight_dict[k]
                                    for k, v in loss_dict_reduced.items() if k in weight_dict}
        losses_reduced_scaled = sum(loss_dict_reduced_scaled.values())

        loss_value = losses_reduced_scaled.item()

        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            print(loss_dict_reduced)
            sys.exit(1)
        optimizer.zero_grad()
        losses.backward()
        if max_norm > 0:
            grad_total_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm)
        else:
            grad_total_norm = utils.get_total_grad_norm(model.parameters(), max_norm)
        optimizer.step()

        metric_logger.update(loss=loss_value, **loss_dict_reduced_scaled, **loss_dict_reduced_unscaled)
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])
        metric_logger.update(grad_norm=grad_total_norm)

        for k in loss_dict.keys():
            writer.add_scalar(str(k), loss_dict[k].cpu().detach().item(), len(data_loader)*epoch + n_iters)
        writer.add_scalar('lr', optimizer.param_groups[0]['lr'], len(data_loader)*epoch + n_iters)
        n_iters += 1

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


@torch.no_grad()
def evaluate(model, criterion, postprocessors, data_loader, evaluator_list, device, args):
    model.eval()
    criterion.eval()

    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'

    for samples, targets in metric_logger.log_every(data_loader, 10, header):
        samples = samples.to(device)
        captions = [t["caption"] for t in targets]
        targets = utils.targets_to(targets, device)

        outputs = model(samples, captions, targets)
        loss_dict = criterion(outputs, targets)
        weight_dict = criterion.weight_dict

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        loss_dict_reduced_scaled = {k: v * weight_dict[k]
                                    for k, v in loss_dict_reduced.items() if k in weight_dict}
        loss_dict_reduced_unscaled = {f'{k}_unscaled': v
                                      for k, v in loss_dict_reduced.items()}
        metric_logger.update(loss=sum(loss_dict_reduced_scaled.values()),
                             **loss_dict_reduced_scaled,
                             **loss_dict_reduced_unscaled)

        orig_target_sizes = torch.stack([t["orig_size"] for t in targets], dim=0)
        results = postprocessors['bbox'](outputs, orig_target_sizes)
        if 'segm' in postprocessors.keys():
            target_sizes = torch.stack([t["size"] for t in targets], dim=0)
            results = postprocessors['segm'](results, outputs, orig_target_sizes, target_sizes)
        res = {target['image_id'].item(): output for target, output in zip(targets, results)}
        
        for evaluator in evaluator_list:
            evaluator.update(res)

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    for evaluator in evaluator_list:
        evaluator.synchronize_between_processes()

    # accumulate predictions from all images
    refexp_res = None
    for evaluator in evaluator_list:
        if isinstance(evaluator, CocoEvaluator):
            evaluator.accumulate()
            evaluator.summarize()
        elif isinstance(evaluator, RefExpEvaluator):
            refexp_res = evaluator.summarize()

    stats = {k: meter.global_avg for k, meter in metric_logger.meters.items()}

    # update stats
    for evaluator in evaluator_list:
        if isinstance(evaluator, CocoEvaluator):
            if "bbox" in postprocessors.keys():
                stats["coco_eval_bbox"] = evaluator.coco_eval["bbox"].stats.tolist()
            if "segm" in postprocessors.keys():
                stats["coco_eval_masks"] = evaluator.coco_eval["segm"].stats.tolist()
    if refexp_res is not None:
        stats.update(refexp_res)
        
    return stats


@torch.no_grad()
def evaluate_a2d(model, data_loader, postprocessor, device, args):
    model.eval()
    predictions = []
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'

    for samples, targets in metric_logger.log_every(data_loader, 10, header):
        image_ids = [t['image_id'] for t in targets]

        samples = samples.to(device)
        captions = [t["caption"] for t in targets]
        targets = utils.targets_to(targets, device)

        outputs = model(samples, captions, targets)

        orig_target_sizes = torch.stack([t["orig_size"] for t in targets], dim=0)
        target_sizes = torch.stack([t["size"] for t in targets], dim=0)
        processed_outputs = postprocessor(outputs, orig_target_sizes, target_sizes)

        # get the best-matched segmentation
        max_idx = processed_outputs[0]['scores'].max(-1)[1]
        predictions.append({ 'image_id': image_ids[0], 'category_id': 1, 'segmentation': processed_outputs[0]['rle_masks'][max_idx],
                             'score': processed_outputs[0]['scores'][max_idx].item()})

        # for p, image_id in zip(processed_outputs, image_ids):
        #     for s, m in zip(p['scores'], p['rle_masks']):
        #             predictions.append({'image_id': image_id,
        #                                 'category_id': 1,  # dummy label, as categories are not predicted in ref-vos
        #                                 'segmentation': m,
        #                                 'score': s.item()})
    
    # gather and merge predictions from all gpus
    gathered_pred_lists = utils.all_gather(predictions)
    predictions = [p for p_list in gathered_pred_lists for p in p_list]
    # evaluation
    eval_metrics = {}
    if utils.is_main_process():
        if args.dataset_file == 'a2d':
            coco_gt = COCO(os.path.join(args.a2d_path, 'a2d_sentences_test_annotations_in_coco_format.json'))
        elif args.dataset_file == 'jhmdb':
            coco_gt = COCO(os.path.join(args.jhmdb_path, 'jhmdb_sentences_gt_annotations_in_coco_format.json'))
        else:
            raise NotImplementedError
        coco_pred = coco_gt.loadRes(predictions)
        coco_eval = COCOeval(coco_gt, coco_pred, iouType='segm')
        coco_eval.params.useCats = 0  # ignore categories as they are not predicted in ref-vos task
        coco_eval.evaluate()
        coco_eval.accumulate()
        coco_eval.summarize()
        ap_labels = ['mAP 0.5:0.95', 'AP 0.5', 'AP 0.75', 'AP 0.5:0.95 S', 'AP 0.5:0.95 M', 'AP 0.5:0.95 L']
        ap_metrics = coco_eval.stats[:6]
        eval_metrics = {l: m for l, m in zip(ap_labels, ap_metrics)}
        # Precision and IOU
        precision_at_k, overall_iou, mean_iou = calculate_precision_at_k_and_iou_metrics(coco_gt, coco_pred)
        eval_metrics.update({f'P@{k}': m for k, m in zip([0.5, 0.6, 0.7, 0.8, 0.9], precision_at_k)})
        eval_metrics.update({'overall_iou': overall_iou, 'mean_iou': mean_iou})
        print(eval_metrics)

    # sync all processes before starting a new epoch or exiting
    dist.barrier()
    return eval_metrics


@torch.no_grad()
def evaluate_online_a2d(model, data_loader, postprocessor, device, args):
    model.eval()
    predictions = []
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'

    for samples, targets in metric_logger.log_every(data_loader, 10, header):
        image_ids = [t['image_id'] for t in targets]

        samples = samples.to(device)
        captions = [t["caption"] for t in targets]
        targets = utils.targets_to(targets, device)

        outputs = model(samples, captions, targets, val=True)

        orig_target_sizes = torch.stack([t["orig_size"] for t in targets], dim=0)
        target_sizes = torch.stack([t["size"] for t in targets], dim=0)
        for output_i, output in enumerate(outputs): # we write prediction for each clip
            processed_output = postprocessor(output, orig_target_sizes, target_sizes)
            max_idx = processed_output[0]['scores'].max(-1)[1]
            predictions.append(
                {'image_id': image_ids[0][output_i], 'category_id': 1, 'segmentation': processed_output[0]['rle_masks'][max_idx],
                 'score': processed_output[0]['scores'][max_idx].item()})
            # p = processed_output[0]
            # image_id = image_ids[0][output_i]
            # for s, m in zip(p['scores'], p['rle_masks']):
            #     predictions.append({'image_id': image_id,
            #                         'category_id': 1,  # dummy label, as categories are not predicted in ref-vos
            #                         'segmentation': m,
            #                         'score': s.item()})

    # gather and merge predictions from all gpus
    gathered_pred_lists = utils.all_gather(predictions)
    predictions = [p for p_list in gathered_pred_lists for p in p_list]
    # evaluation
    eval_metrics = {}
    if utils.is_main_process():
        if args.dataset_file == 'a2d':
            coco_gt = COCO(os.path.join(args.a2d_path, 'a2d_sentences_test_annotations_in_coco_format.json'))
        elif args.dataset_file == 'jhmdb':
            coco_gt = COCO(os.path.join(args.jhmdb_path, 'jhmdb_sentences_gt_annotations_in_coco_format.json'))
        else:
            raise NotImplementedError
        coco_pred = coco_gt.loadRes(predictions)
        coco_eval = COCOeval(coco_gt, coco_pred, iouType='segm')
        coco_eval.params.useCats = 0  # ignore categories as they are not predicted in ref-vos task
        coco_eval.evaluate()
        coco_eval.accumulate()
        coco_eval.summarize()
        ap_labels = ['mAP 0.5:0.95', 'AP 0.5', 'AP 0.75', 'AP 0.5:0.95 S', 'AP 0.5:0.95 M', 'AP 0.5:0.95 L']
        ap_metrics = coco_eval.stats[:6]
        eval_metrics = {l: m for l, m in zip(ap_labels, ap_metrics)}
        # Precision and IOU
        precision_at_k, overall_iou, mean_iou = calculate_precision_at_k_and_iou_metrics(coco_gt, coco_pred)
        eval_metrics.update({f'P@{k}': m for k, m in zip([0.5, 0.6, 0.7, 0.8, 0.9], precision_at_k)})
        eval_metrics.update({'overall_iou': overall_iou, 'mean_iou': mean_iou})
        print(eval_metrics)

    # sync all processes before starting a new epoch or exiting
    dist.barrier()
    return eval_metrics


@torch.no_grad()
def evaluate_a2d_g_sam(sam_predictor, inferencer, dataset_val, device, args):
    predictions = []
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'
    postprocessors = build_postprocessors(args, args.dataset_file)

    def get_image_id(video_id, frame_idx, ref_instance_a2d_id):
        image_id = f'v_{video_id}_f_{frame_idx}_i_{ref_instance_a2d_id}'
        return image_id
    
    curr_video_id = None
    prop_idx = 0
    for idx, val in enumerate(metric_logger.log_every(dataset_val, 100, header)):
        text_query, video_id, frame_idx, instance_id = dataset_val.text_annotations[idx]
        image_id = get_image_id(video_id,frame_idx, instance_id)
        
        if curr_video_id != video_id:
            curr_video_id = video_id
            prop_idx = 0
        
        # print(frame_idx)
        text_query = " ".join(text_query.lower().split())  # clean up the text query
        # read the source window frames:
        video_frames, _, _ = read_video(os.path.join(dataset_val.videos_dir, f'{video_id}.mp4'), pts_unit='sec')  # (T, H, W, C)
        # video_frames = mmcv.VideoReader(os.path.join(self.videos_dir, f'{video_id}.mp4'))
        # vid_len = len(video_frames)
        # note that the original a2d dataset is 1 indexed, so we have to subtract 1 from frame_idx
        frame_id = frame_idx - 1

        start_idx, end_idx = frame_id - args.num_frames // 2, frame_id + (args.num_frames + 1) // 2
        sample_indx = []
        for i in range(start_idx, end_idx):
            i = min(max(i, 0), len(video_frames)-1)  # pad out of range indices with edge frames
            sample_indx.append(i)
        sample_indx.sort()
        # find the valid frame index in sampled frame list, there is only one valid frame
        # valid_indices = sample_indx.index(frame_id)
        # read frames 
        
        for j in range(args.num_frames):
            frame_indx = sample_indx[j]
            ## TODO: test rgb bgr because we dont use mmcv here
            img = cv2.cvtColor(video_frames[frame_indx].cpu().numpy(), cv2.COLOR_RGB2BGR)
            # img = torch.tensor(img).permute(2,0,1)
            # img = video_frames[frame_indx].permute(2, 0, 1)
            # img = F.to_pil_image(video_frames[frame_indx].permute(2, 0, 1))
            # imgs.append(img)
        # G-DINO
        result = inferencer(img, texts=text_query, frame_idx=prop_idx)
        logits = torch.tensor(result['predictions'][0]['scores'])
        boxes = torch.tensor(result['predictions'][0]['bboxes'])
        prop_idx += 1

        ################ TODO: workaround 
        # Thresholding
        threshold = args.gdino_thres
        thres = logits > threshold
        indices = torch.nonzero(thres).squeeze(-1)
        if len(indices) == 0:
            boxes = []
        else:
            boxes = boxes[indices]
            logits = logits[indices]
        ################ TODO: workaround 

        # Note!!! handle special cases for "india"
        if len(boxes) == 0:
            boxes_xyxy = torch.zeros((1, 4))
            masks = torch.zeros(masks.shape).to(device)
        else:
            max_logit, max_idx = torch.max(logits, dim=0)
            boxes = boxes[max_idx].unsqueeze(0) ## shape: (1, 4)
            sam_predictor.set_image(img, image_format='BGR')
            boxes_xyxy = boxes
        
            transformed_boxes = sam_predictor.transform.apply_boxes_torch(boxes_xyxy, img.shape[:2]).to(device)
            masks, _, _ = sam_predictor.predict_torch(
                        point_coords = None,
                        point_labels = None,
                        boxes = transformed_boxes,
                        multimask_output = False,
                    )
           
        size = torch.tensor([img.shape[:2]]).cuda()
        pred = postprocessors(masks, size, size)
        
        for val in pred:
            for m in val['rle_masks']:
                predictions.append({'image_id': image_id, 'category_id': 1, 'segmentation': m,
                                    'score': max_logit})
    
    # gather and merge predictions from all gpus
    gathered_pred_lists = utils.all_gather(predictions)
    predictions = [p for p_list in gathered_pred_lists for p in p_list]
    # evaluation
    eval_metrics = {}
    if utils.is_main_process():
        if args.dataset_file == 'a2d':
            coco_gt = COCO(os.path.join(args.a2d_path, 'a2d_sentences_test_annotations_in_coco_format.json'))
        elif args.dataset_file == 'jhmdb':
            coco_gt = COCO(os.path.join(args.jhmdb_path, 'jhmdb_sentences_gt_annotations_in_coco_format.json'))
        else:
            raise NotImplementedError
        coco_pred = coco_gt.loadRes(predictions)
        coco_eval = COCOeval(coco_gt, coco_pred, iouType='segm')
        coco_eval.params.useCats = 0  # ignore categories as they are not predicted in ref-vos task
        coco_eval.evaluate()
        coco_eval.accumulate()
        coco_eval.summarize()
        ap_labels = ['mAP 0.5:0.95', 'AP 0.5', 'AP 0.75', 'AP 0.5:0.95 S', 'AP 0.5:0.95 M', 'AP 0.5:0.95 L']
        ap_metrics = coco_eval.stats[:6]
        eval_metrics = {l: m for l, m in zip(ap_labels, ap_metrics)}
        # Precision and IOU
        precision_at_k, overall_iou, mean_iou = calculate_precision_at_k_and_iou_metrics(coco_gt, coco_pred)
        eval_metrics.update({f'P@{k}': m for k, m in zip([0.5, 0.6, 0.7, 0.8, 0.9], precision_at_k)})
        eval_metrics.update({'overall_iou': overall_iou, 'mean_iou': mean_iou})
        print(eval_metrics)

    # sync all processes before starting a new epoch or exiting
    # dist.barrier()
    return eval_metrics


@torch.no_grad()
def evaluate_jhmdb_g_sam(sam_predictor, inferencer, dataset_val, device, args):
    predictions = []
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'
    postprocessors = build_postprocessors(args, args.dataset_file)

    def get_image_id(video_id, frame_idx):
        image_id = f'v_{video_id}_f_{frame_idx}'
        return image_id
    
    curr_video_id = None
    prop_idx = 0
    for idx, val in enumerate(metric_logger.log_every(dataset_val, 100, header)):
      
        video_id, chosen_frame_path, video_masks_path, video_total_frames, text_query = dataset_val.samples_metadata[idx]
        if curr_video_id != video_id:
            curr_video_id = video_id
            prop_idx = 0
        # print(frame_idx)
        text_query = " ".join(text_query.lower().split())  # clean up the text query
        # read the source window frames:
        chosen_frame_idx = int(chosen_frame_path.split('/')[-1].split('.')[0])
        # get a window of window_size frames with frame chosen_frame_idx in the middle.
        start_idx, end_idx = chosen_frame_idx - args.num_frames // 2, chosen_frame_idx + (args.num_frames + 1) // 2
        frame_indices = list(range(start_idx, end_idx))  # note that jhmdb-sentences frames are 1-indexed
        # extract the window source frames:
        sample_indx = []
        for i in frame_indices:
            i = min(max(i, 1), video_total_frames)  # pad out of range indices with edge frames
            sample_indx.append(i)
        sample_indx.sort()
        # find the valid frame index in sampled frame list, there is only one valid frame
        # read frames 
        image_id = get_image_id(video_id, chosen_frame_idx)
        # read frames
        for i in sample_indx:
            p = '/'.join(chosen_frame_path.split('./jhmdb_sentences')[1].split('/')[:-1]) + f'/{i:05d}.png'
            # frame_path = os.path.join(self.dataset_path, p)
            frame_path = dataset_val.dataset_path + p
            # imgs.append(Image.open(frame_path).convert('RGB'))
            img = mmcv.imread(frame_path)
        # G-DINO
        result = inferencer(img, texts=text_query, frame_idx=prop_idx) # NOTE: frame_idx is dummy
        logits = torch.tensor(result['predictions'][0]['scores'])
        boxes = torch.tensor(result['predictions'][0]['bboxes'])
        prop_idx += 1

        ################ TODO: workaround 
        # Thresholding
        threshold = args.gdino_thres
        thres = logits > threshold
        indices = torch.nonzero(thres).squeeze(-1)
        if len(indices) == 0:
            boxes = []
        else:
            boxes = boxes[indices]
            logits = logits[indices]
        ################ TODO: workaround 

        # Note!!! handle special cases for "india"
        if len(boxes) == 0:
            boxes_xyxy = torch.zeros((1, 4))
            masks = torch.zeros(masks.shape).to(device)
        else:
            max_logit, max_idx = torch.max(logits, dim=0)
            boxes = boxes[max_idx].unsqueeze(0) ## shape: (1, 4)
            sam_predictor.set_image(img, image_format='BGR')
            boxes_xyxy = boxes
        
            transformed_boxes = sam_predictor.transform.apply_boxes_torch(boxes_xyxy, img.shape[:2]).to(device)
            masks, _, _ = sam_predictor.predict_torch(
                        point_coords = None,
                        point_labels = None,
                        boxes = transformed_boxes,
                        multimask_output = False,
                    )
           
        size = torch.tensor([img.shape[:2]]).cuda()
        pred = postprocessors(masks, size, size)
        
        for val in pred:
            for m in val['rle_masks']:
                predictions.append({'image_id': image_id, 'category_id': 1, 'segmentation': m,
                                    'score': max_logit})
    
    # gather and merge predictions from all gpus
    gathered_pred_lists = utils.all_gather(predictions)
    predictions = [p for p_list in gathered_pred_lists for p in p_list]
    # evaluation
    eval_metrics = {}
    if utils.is_main_process():
        if args.dataset_file == 'a2d':
            coco_gt = COCO(os.path.join(args.a2d_path, 'a2d_sentences_test_annotations_in_coco_format.json'))
        elif args.dataset_file == 'jhmdb':
            coco_gt = COCO(os.path.join(args.jhmdb_path, 'jhmdb_sentences_gt_annotations_in_coco_format.json'))
        else:
            raise NotImplementedError
        coco_pred = coco_gt.loadRes(predictions)
        coco_eval = COCOeval(coco_gt, coco_pred, iouType='segm')
        coco_eval.params.useCats = 0  # ignore categories as they are not predicted in ref-vos task
        coco_eval.evaluate()
        coco_eval.accumulate()
        coco_eval.summarize()
        ap_labels = ['mAP 0.5:0.95', 'AP 0.5', 'AP 0.75', 'AP 0.5:0.95 S', 'AP 0.5:0.95 M', 'AP 0.5:0.95 L']
        ap_metrics = coco_eval.stats[:6]
        eval_metrics = {l: m for l, m in zip(ap_labels, ap_metrics)}
        # Precision and IOU
        precision_at_k, overall_iou, mean_iou = calculate_precision_at_k_and_iou_metrics(coco_gt, coco_pred)
        eval_metrics.update({f'P@{k}': m for k, m in zip([0.5, 0.6, 0.7, 0.8, 0.9], precision_at_k)})
        eval_metrics.update({'overall_iou': overall_iou, 'mean_iou': mean_iou})
        print(eval_metrics)

    # sync all processes before starting a new epoch or exiting
    # dist.barrier()
    return eval_metrics


@torch.no_grad()
def evaluate_a2d_g_sam_gtbbox(sam_predictor, data_loader, device, args):
    predictions = []
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'
    cnt = 0
    postprocessors = build_postprocessors(args, args.dataset_file)
    for samples, targets in metric_logger.log_every(data_loader, 100, header):
        
        image_ids = [t['image_id'] for t in targets]

        samples = samples.to(device)
        captions = [t["caption"] for t in targets]
        targets = utils.targets_to(targets, device)
        img_input = samples.tensors.cpu().numpy().squeeze().transpose(1,2,0)

        # (h, w, 3)
        boxes = targets[0]['boxes']
        # Note!!! handle special cases for "india"
        if len(boxes) == 0:
            boxes_xyxy = torch.zeros((1, 4))
            ### DONE:
            # if this situation, no need SAM! (just all zeros)
            masks = torch.zeros(masks.shape).to(device)
            # pred_masks.append(masks)
            # pred_boxes.append(boxes_xyxy)
            # pred_logits.append(torch.zeros((1,)))
        else:
            sam_predictor.set_image(img_input, image_format='BGR')

            boxes_xyxy = boxes
        
            transformed_boxes = sam_predictor.transform.apply_boxes_torch(boxes_xyxy, img_input.shape[:2]).to(device)
            # print(transformed_boxes)
            # print(transformed_boxes.shape)
            masks, _, _ = sam_predictor.predict_torch(
                        point_coords = None,
                        point_labels = None,
                        boxes = transformed_boxes,
                        multimask_output = False,
                    )
            # print(f'{frame} {masks.shape}')
            # print(f'obj: {obj_id}, t: {t}, box shape: {masks.shape}')
            # pred_masks.append(masks)
            # pred_boxes.append(boxes_xyxy)
            # pred_logits.append(max_logit.unsqueeze(0))
        # outputs = model(samples, captions, targets)
        orig_target_sizes = torch.stack([t["orig_size"] for t in targets], dim=0)
        target_sizes = torch.stack([t["size"] for t in targets], dim=0)
        # !!!!
        # masks = targets[0]['masks'].unsqueeze(0)
        
        pred = postprocessors(masks, orig_target_sizes, target_sizes)
        # processed_outputs = postprocessor(outputs, orig_target_sizes, target_sizes)

        # get the best-matched segmentation
        # max_idx = processed_outputs[0]['scores'].max(-1)[1]
        for val in pred:
            for m in val['rle_masks']:
                predictions.append({'image_id': image_ids[0], 'category_id': 1, 'segmentation': m,
                                    'score': 0})
        # for p, image_id in zip(processed_outputs, image_ids):
        #     for s, m in zip(p['scores'], p['rle_masks']):
        #             predictions.append({'image_id': image_id,
        #                                 'category_id': 1,  # dummy label, as categories are not predicted in ref-vos
        #                                 'segmentation': m,
        #                                 'score': s.item()})
    # gather and merge predictions from all gpus
    gathered_pred_lists = utils.all_gather(predictions)
    predictions = [p for p_list in gathered_pred_lists for p in p_list]
    # evaluation
    eval_metrics = {}
    if utils.is_main_process():
        if args.dataset_file == 'a2d':
            coco_gt = COCO(os.path.join(args.a2d_path, 'a2d_sentences_test_annotations_in_coco_format.json'))
        elif args.dataset_file == 'jhmdb':
            coco_gt = COCO(os.path.join(args.jhmdb_path, 'jhmdb_sentences_gt_annotations_in_coco_format.json'))
        else:
            raise NotImplementedError
        coco_pred = coco_gt.loadRes(predictions)
        coco_eval = COCOeval(coco_gt, coco_pred, iouType='segm')
        coco_eval.params.useCats = 0  # ignore categories as they are not predicted in ref-vos task
        coco_eval.evaluate()
        coco_eval.accumulate()
        coco_eval.summarize()
        ap_labels = ['mAP 0.5:0.95', 'AP 0.5', 'AP 0.75', 'AP 0.5:0.95 S', 'AP 0.5:0.95 M', 'AP 0.5:0.95 L']
        ap_metrics = coco_eval.stats[:6]
        eval_metrics = {l: m for l, m in zip(ap_labels, ap_metrics)}
        # Precision and IOU
        precision_at_k, overall_iou, mean_iou = calculate_precision_at_k_and_iou_metrics(coco_gt, coco_pred)
        eval_metrics.update({f'P@{k}': m for k, m in zip([0.5, 0.6, 0.7, 0.8, 0.9], precision_at_k)})
        eval_metrics.update({'overall_iou': overall_iou, 'mean_iou': mean_iou})
        print(eval_metrics)

    # sync all processes before starting a new epoch or exiting
    # dist.barrier()
    return eval_metrics






