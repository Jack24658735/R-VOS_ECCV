'''
Inference code for OnlineRefer, on Ref-DAVIS17
Modified from ReferFormer (https://github.com/wjn922/ReferFormer)
Ref-Davis17 does not support visualize
'''
import argparse
import json
import random
import time
from pathlib import Path

import numpy as np
import torch

import util.misc as utils
# from models import build_model
import torchvision.transforms as T
import matplotlib.pyplot as plt
import os
import cv2
from PIL import Image, ImageDraw, ImageFont
import math
import torch.nn.functional as F
import json

import opts
from tqdm import tqdm

import multiprocessing as mp
import threading

from tools_refer.colormap import colormap

from huggingface_hub import hf_hub_download

# Grounding DINO
# import GroundingDINO.groundingdino.datasets.transforms as T
from GroundingDINO.groundingdino.util import box_ops
from GroundingDINO.groundingdino.util.slconfig import SLConfig
from GroundingDINO.groundingdino.util.utils import clean_state_dict, get_phrases_from_posmap
from GroundingDINO.groundingdino.util.inference import annotate, load_image, predict

import csv
import transformers

transformers.logging.set_verbosity(transformers.logging.ERROR)


DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# colormap
color_list = colormap()
color_list = color_list.astype('uint8').tolist()

# build transform
transform = T.Compose([
    T.Resize(360),
    T.ToTensor(),
    T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
])

### Settings for Grounding Dino & SAM
# GroundingDINO config and checkpoint
# GROUNDING_DINO_CONFIG_PATH = "GroundingDINO/groundingdino/config/GroundingDINO_SwinT_OGC.py"
# GROUNDING_DINO_CHECKPOINT_PATH = "./groundingdino_swint_ogc.pth"

BOX_THRESHOLD = 0.3
TEXT_THRESHOLD = 0.25

def load_model_hf(args, repo_id, filename, ckpt_config_filename, device='cpu'):
    # particularly import build_model since the function name is same as onlinerefer
    from GroundingDINO.groundingdino.models import build_model
    cache_config_file = hf_hub_download(repo_id=repo_id, filename=ckpt_config_filename)
   
    args_gdino = SLConfig.fromfile(cache_config_file) 
    model = build_model(args_gdino)
    args_gdino.device = device

    cache_file = hf_hub_download(repo_id=repo_id, filename=filename)
    checkpoint = torch.load(cache_file, map_location='cpu')
    log = model.load_state_dict(clean_state_dict(checkpoint['model']), strict=False)
    print("Model loaded from {} \n => {}".format(cache_file, log))
    _ = model.eval()
    return model   

def main(args):
    args.dataset_file = "davis"
    args.masks = True
    args.batch_size == 1
    print("Inference only supports for batch size = 1")
    if args.use_trained_gdino:
        print(f'*** Loaded from {args.g_dino_ckpt_path} ***')

    # fix the seed for reproducibility
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)

    split = args.split
    # save path
    output_dir = args.output_dir
    import ipdb;
    ipdb.set_trace()
    save_path_prefix = os.path.join(output_dir, split)
    if not os.path.exists(save_path_prefix):
        os.makedirs(save_path_prefix)

    save_visualize_path_prefix = os.path.join(output_dir, split + '_images')
    if args.visualize:
        if not os.path.exists(save_visualize_path_prefix):
            os.makedirs(save_visualize_path_prefix)

    # load data
    root = Path(args.davis_path)  # data/ref-davis
    img_folder = os.path.join(root, split, "JPEGImages")
    meta_file = os.path.join(root, "meta_expressions", split, "meta_expressions.json")
    with open(meta_file, "r") as f:
        data = json.load(f)["videos"]
    video_list = list(data.keys())

    # create subprocess
    thread_num = args.ngpu
    global result_dict
    result_dict = mp.Manager().dict()

    processes = []
    lock = threading.Lock()

    video_num = len(video_list)
    per_thread_video_num = math.ceil(float(video_num) / float(thread_num))

    start_time = time.time()
    print('Start inference')
    ### Note: workaround to avoid the multi-process issue...
    sub_video_list = video_list[0:]
    sub_processor(lock, 0, args, data, save_path_prefix, save_visualize_path_prefix, img_folder, sub_video_list)

    # for i in range(thread_num):
    #     if i == thread_num - 1:
    #         sub_video_list = video_list[i * per_thread_video_num:]
    #     else:
    #         sub_video_list = video_list[i * per_thread_video_num: (i + 1) * per_thread_video_num]
    #     p = mp.Process(target=sub_processor, args=(lock, i, args, data,
    #                                                save_path_prefix, save_visualize_path_prefix,
    #                                                img_folder, sub_video_list))
    #     p.start()
    #     processes.append(p)

    # for p in processes:
    #     p.join()

    end_time = time.time()
    total_time = end_time - start_time

    result_dict = dict(result_dict)
    num_all_frames_gpus = 0
    for pid, num_all_frames in result_dict.items():
        num_all_frames_gpus += num_all_frames

    print("Total inference time: %.4f s" % (total_time))


# Define a function to save bounding box data to a CSV file
def save_bbox_to_csv(output_csv, data):
    # Check if the file exists to decide whether to write headers
    write_headers = not os.path.exists(output_csv)

    with open(output_csv, 'a', newline='') as file:
        fieldnames = ["VideoName", "ImageName", "ObjectID", "xmin", "ymin", "xmax", "ymax", "Confidence", "Class"]
        writer = csv.DictWriter(file, fieldnames=fieldnames)

        if write_headers:
            writer.writeheader()

        for entry in data:
            writer.writerow(entry)



def sub_processor(lock, pid, args, data, save_path_prefix, save_visualize_path_prefix, img_folder, video_list):
    text = 'processor %d' % pid
    with lock:
        progress = tqdm(
            total=len(video_list),
            position=pid,
            desc=text,
            ncols=0
        )
    torch.cuda.set_device(pid)
    ## 1. Build Grounding dino
    # Use this command for evaluate the Grounding DINO model
    # Or you can download the model by yourself
    ckpt_repo_id = "ShilongLiu/GroundingDINO"
    # ckpt_filenmae = "groundingdino_swint_ogc.pth"
    
    # ckpt_config_filename = "GroundingDINO_SwinT_OGC.cfg.py"
    ckpt_filenmae = "groundingdino_swinb_cogcoor.pth"
    ckpt_config_filename = "GroundingDINO_SwinB.cfg.py"
    # grounding_dino_model = Model(model_config_path=GROUNDING_DINO_CONFIG_PATH, model_checkpoint_path=GROUNDING_DINO_CHECKPOINT_PATH)
    grounding_dino_model = load_model_hf(args, ckpt_repo_id, ckpt_filenmae, ckpt_config_filename)

    ##load again for our trained G-DINO
    if args.use_trained_gdino:
        checkpoint = torch.load(args.g_dino_ckpt_path, map_location='cpu')
        log = grounding_dino_model.load_state_dict(clean_state_dict(checkpoint['model']), strict=False)
        print("Model loaded from {} \n => {}".format(args.g_dino_ckpt_path, log))
        _ = grounding_dino_model.eval()

    device = torch.device('cuda:0')

    # get palette
    palette_img = os.path.join(args.davis_path, "valid/Annotations/blackswan/00000.png")
    palette = Image.open(palette_img).getpalette()

    num_all_frames = 0

    font_path = "fonts/OpenSans-Regular.ttf"
    font = ImageFont.truetype(font_path, 30) # change the '30' to any size you want
    print(f'*** Num of anno. we run for DAVIS: {args.run_anno_id}***')
    # 1. for each video
    for video in video_list:
        metas = []

        expressions = data[video]["expressions"]
        expression_list = list(expressions.keys())
        num_expressions = len(expression_list)
        video_len = len(data[video]["frames"])

        # read all the anno meta
        for i in range(num_expressions):
            meta = {}
            meta["video"] = video
            meta["exp"] = expressions[expression_list[i]]["exp"]
            meta["exp_id"] = expression_list[i]  # start from 0
            meta["frames"] = data[video]["frames"]
            metas.append(meta)
        meta = metas

        # since there are 4 annotations
        num_obj = num_expressions // 4
        image_cache_for_video = {}
        # 2. for each annotator
        for anno_id in range(args.run_anno_id):  # 4 annotators
            all_exps = []
            bbox_data = []
            anno_logits = []
            anno_boxes = []
            for obj_id in range(num_obj):
                i = obj_id * 4 + anno_id
                video_name = meta[i]["video"]
                exp = meta[i]["exp"]
                exp_id = meta[i]["exp_id"]
                frames = meta[i]["frames"]

                video_len = len(frames)
                # NOTE: the im2col_step for MSDeformAttention is set as 64
                # so the max length for a clip is 64
                # store the video pred results
                all_pred_logits = []
                all_pred_boxes = []

                if args.semi_online:
                    num_clip_frames = args.num_frames
                else:
                    num_clip_frames = 1

                # 3. for each clip
                # track_res = model.generate_empty_tracks()
                for clip_id in range(0, video_len, num_clip_frames):
                    frames_ids = [x for x in range(video_len)]
                    clip_frames_ids = frames_ids[clip_id: clip_id + num_clip_frames]
                    # load the clip images
                    imgs = []
                    pred_logits = []
                    pred_boxes = []
                    for t in clip_frames_ids:
                        frame = frames[t]
                        img_path = os.path.join(img_folder, video_name, frame + ".jpg")
                        
                        # Use the cached image if it's already loaded, otherwise load and cache it
                        if img_path not in image_cache_for_video:
                            image_cache_for_video[img_path] = Image.open(img_path).convert('RGB')
                        img = image_cache_for_video[img_path].copy()
                        imgs.append(transform(img))  # list[Img]

                        ## TODO: maybe directly perform prediction here? 
                        # so we can avoid the batch inference issues..
                        with torch.no_grad(): #### REMEMBER ADD THIS when infernce
                            # TODO: perform detection with Grounding DINO
                            # detect objects
                            boxes, logits, phrases, _ = predict(
                                model=grounding_dino_model, 
                                image=transform(img), 
                                caption=exp,
                                box_threshold=BOX_THRESHOLD, 
                                text_threshold=TEXT_THRESHOLD
                            )
                            # Note!!! handle special cases for "india"
                            if torch.numel(boxes) == 0:
                                boxes_xyxy = torch.zeros((1, 4))
                                ### DONE:
                                # if this situation, no need SAM! (just all zeros)
                                pred_boxes.append(boxes_xyxy)
                                pred_logits.append(torch.zeros((1,)))
                            else:
                                max_logit, max_idx = torch.max(logits, dim=0)
                                boxes = boxes[max_idx].unsqueeze(0) ## shape: (1, 4)
                                img_arr = np.asarray(img)
                                H, W, _ = img_arr.shape
                                boxes_xyxy = box_ops.box_cxcywh_to_xyxy(boxes) * torch.Tensor([W, H, W, H])
                                # # box: normalized box xywh -> unnormalized xyxy
                                pred_boxes.append(boxes_xyxy)
                                pred_logits.append(max_logit.unsqueeze(0))
                              
                    pred_boxes = torch.cat(pred_boxes, dim=0)
                    ### save boxes
                    pred_logits = torch.cat(pred_logits, dim=0)
                    all_pred_boxes.append(pred_boxes)
                    all_pred_logits.append(pred_logits)

                all_pred_logits = torch.cat(all_pred_logits, dim=0)  # (video_len, K)
                anno_logits.append(all_pred_logits)
            
                all_exps.append(exp)
                all_pred_boxes = torch.cat(all_pred_boxes, dim=0)
                anno_boxes.append(all_pred_boxes)

                # handle a complete image (all objects of a annotator)
            anno_logits = torch.stack(anno_logits)  # [num_obj, video_len, k]
            # save results
            anno_save_path = os.path.join(save_path_prefix, f"anno_{anno_id}", video)
            if not os.path.exists(anno_save_path):
                os.makedirs(anno_save_path)
            
            for f in range(anno_boxes[0].shape[0]):
                # print(out_masks[f].shape)
                # img_E = Image.fromarray(out_masks[f])
                # img_E.putpalette(palette)
                # img_E.save(os.path.join(anno_save_path, '{:05d}.png'.format(f)))

                if args.visualize:
                    exp_idx = 0
                    # for k in range(num_obj, 0, -1):
                    image_cache = {}
                    for k in range(1, num_obj + 1):
                        img_path = os.path.join(img_folder, video_name, frames[f] + ".jpg")
                        # Use the cached image if it's already loaded, otherwise load and cache it
                        if img_path not in image_cache:
                            image_cache[img_path] = Image.open(img_path).convert('RGBA')

                        # source_img = Image.open(img_path).convert('RGBA')
                        source_img = image_cache[img_path].copy()
                        origin_w, origin_h = source_img.size
                        draw = ImageDraw.Draw(source_img)
                        # text = expressions[expression_list[i]]["exp"]
                        # Example: bike-packing => ['a black bike', 'a man wearing a cap']
                        # print(all_exps)
                        # print(len(all_exps))
                        text = all_exps[exp_idx]
                        position = (10, 10)
                        draw.text(position, text, (255, 0, 0), font=font)

                        
                        draw_boxes = anno_boxes[k - 1][f].unsqueeze(0)
                        # draw_boxes = rescale_bboxes(draw_boxes.detach(), (origin_w, origin_h)).tolist()
                        # print(draw_boxes)
                        # print(draw_boxes.shape)
                        xmin, ymin, xmax, ymax = draw_boxes[0]
                        draw.rectangle(((xmin, ymin), (xmax, ymax)), outline=tuple(color_list[k%len(color_list)]), width=2)
                        # plot the bbox score
                        plot_bbox_score = f'{anno_logits[k - 1][f].item():.2f}'
                        draw.text((xmin, ymin), plot_bbox_score, (255, 0, 0), font=font)

                        save_visualize_path_dir = os.path.join(save_visualize_path_prefix, video, str(i))
                        save_visualize_path_dir = os.path.join(save_visualize_path_dir, f'obj_{k}')
                        if not os.path.exists(save_visualize_path_dir):
                            os.makedirs(save_visualize_path_dir)
                        save_visualize_path = os.path.join(save_visualize_path_dir,  frames[f] + f'_{k}.png')
                        source_img.save(save_visualize_path)
                        exp_idx += 1

                        bbox_info = {
                            "VideoName": video_name,
                            "ImageName": frames[f] + ".jpg",
                            "ObjectID": k,
                            "xmin": xmin,
                            "ymin": ymin,
                            "xmax": xmax,
                            "ymax": ymax,
                            "Confidence": anno_logits[k - 1][f].item()
                        }
                        bbox_data.append(bbox_info)
            # Define the path to the output CSV file
            output_csv = os.path.join(anno_save_path, "bounding_box_data.csv")
            # Call the function to save the bounding box data to the CSV file
            save_bbox_to_csv(output_csv, bbox_data)

        with lock:
            progress.update(1)
    result_dict[str(pid)] = num_all_frames
    with lock:
        progress.close()


# Post-process functions
def box_cxcywh_to_xyxy(x):
    x_c, y_c, w, h = x.unbind(1)
    b = [(x_c - 0.5 * w), (y_c - 0.5 * h),
         (x_c + 0.5 * w), (y_c + 0.5 * h)]
    return torch.stack(b, dim=1)


def rescale_bboxes(out_bbox, size):
    img_w, img_h = size
    b = box_cxcywh_to_xyxy(out_bbox)
    b = b.cpu() * torch.tensor([img_w, img_h, img_w, img_h], dtype=torch.float32)
    return b


# Visualization functions
def draw_reference_points(draw, reference_points, img_size, color):
    W, H = img_size
    for i, ref_point in enumerate(reference_points):
        init_x, init_y = ref_point
        x, y = W * init_x, H * init_y
        cur_color = color
        draw.line((x - 10, y, x + 10, y), tuple(cur_color), width=4)
        draw.line((x, y - 10, x, y + 10), tuple(cur_color), width=4)


def draw_sample_points(draw, sample_points, img_size, color_list):
    alpha = 255
    for i, samples in enumerate(sample_points):
        for sample in samples:
            x, y = sample
            cur_color = color_list[i % len(color_list)][::-1]
            cur_color += [alpha]
            draw.ellipse((x - 2, y - 2, x + 2, y + 2),
                         fill=tuple(cur_color), outline=tuple(cur_color), width=1)


def vis_add_mask(img, mask, color):
    origin_img = np.asarray(img.convert('RGB')).copy()
    color = np.array(color)

    mask = mask.reshape(mask.shape[0], mask.shape[1]).astype('uint8')  # np
    mask = mask > 0.5

    origin_img[mask] = origin_img[mask] * 0.5 + color * 0.5
    origin_img = Image.fromarray(origin_img)
    return origin_img


if __name__ == '__main__':
    parser = argparse.ArgumentParser('OnlineRefer inference script', parents=[opts.get_args_parser()])
    args = parser.parse_args()
    main(args)

