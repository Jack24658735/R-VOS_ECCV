
<div align="center">
<h1>
<b>
GroPrompt: Grounded Prompting for Referring Video Object Segmentation
</b>
</h1>
</div>

<p align="center"><iframe src="docs/model.pdf" width="800" height="600" style="border: none;"></iframe></p>



> *GroPrompt: Grounded Prompting for Referring Video Object Segmentation**
>
> Ci-Siang Lin*, I-Jieh Liu*, Min-Hung Chen, Chien-Yi Wang, Sifei Liu, Yu-Chiang Frank Wang

### Abstract

Referring Video Object Segmentation (RVOS) aims to segment the object referred to by the query sentence throughout the entire video. Most existing methods require end-to-end training with dense mask annotations, which could be computation-consuming and less scalable. In this work, we aim to efficiently adapt foundation segmentation models for addressing RVOS from weak supervision with the proposed Grounded Prompting (GroPrompt) framework. More specifically, we propose Text-Aware Prompt Contrastive Learning (TAP-CL) to enhance the association between the position prompts and the referring sentences with only box supervisions, including Text-Contrastive Prompt Learning (TextCon) and Modality-Contrastive Prompt Learning (ModalCon) at frame level and video level, respectively. With the proposed TAP-CL, our GroPrompt framework can generate temporal-consistent yet text-aware position prompts describing locations and movements for the referred object from the video. The experimental results in the standard RVOS benchmarks (Ref-YouTube-VOS, Ref-DAVIS17, A2D-Sentences, and JHMDB-Sentences) demonstrate the competitive performance of our proposed GroPrompt framework given only bounding box weak supervisions.

## Update
- **(2023/11/18)** GroPrompt is under reviewed by CVPR2024.

## Environment setup steps
1. Clone our repo. **https://github.com/Jack24658735/R-VOS**
2. Install conda env. with `conda env create --name *env_name* python=3.8`
3. Setup environment variables for CUDA_HOME (Change to your CUDA version)
    ```bash
    export CUDA_HOME=/usr/local/cuda-11.4
    ```
4. Install PyTorch and Torchvision with -f flag.
    ```bash
    pip install torch==1.12.0+cu116 torchvision==0.13.0+cu116 -f https://download.pytorch.org/whl/torch_stable.html
    ```
    * Note: On TWCC, we should install the newest torch by directly using `pip install torch && pip install torchvision` to avoid errors.
5. Install packages needed for our project.
    ``` bash
    # Install packages from RVOS (Referformer, Onlinerefer)
    # Note: pillow version should be 8.4.0 to avoid error
    pip install -r requirements.txt
    pip install 'git+https://github.com/facebookresearch/fvcore' 
    pip install -U 'git+https://github.com/cocodataset/cocoapi.git#subdirectory=PythonAPI'
    cd models/ops
    # remove build/ and dist/ if they exist
    rm -r build/ && rm -r dist && rm -r MultiScaleDeformableAttention.egg-info/
    python setup.py build install
    cd ../..

    # install SAM
    # Note: if you modify the SAM code, you should re-run this command.
    python -m pip install -e segment_anything
    
    # remove build/ and groundingdino.egg-info/ if they exist
    cd GroundingDINO/
    rm -r build/
    rm -r groundingdino.egg-info/

    # install GroundingDINO
    pip install -e .
    ```
## Extra setup for mmdetection
1. Install mmcv with pip install. **Please be cautious about the version of cuda and torch.**
    ```bash
    pip install "mmcv>=2.0.0" -f https://download.openmmlab.com/mmcv/dist/cu116/torch1.12.0/index.html
    ```
2. Install mmdet & mmengine
    ```bash
    cd mmdetection
    pip install -v -e .
    ```
    ```bash
    cd mmengine
    pip install -v -e .
    ```
3. Also, need to install the packages in `requirements.txt` in grounding_dino folder under mmdet. (multimodal.txt)

# G-DINO

## Run our code in mmdetection (Training)
* Prepare data & model weight (e.g., trained G-DINO checkpoint)
    * Please download from https://download.openmmlab.com/mmdetection/v3.0/grounding_dino/groundingdino_swinb_cogcoor_mmdet-55949c9c.pth
    * The saved path should be `"./R-VOS/mm_weights/groundingdino_swinb_cogcoor_mmdet-55949c9c.pth"` (if you change it, you should refer to the path in the config file, which is under `"./R-VOS/mmdetection/configs/grounding_dino/grounding_dino_swin-b_rvos.py"`)
* Run the following command:
    ```bash
    # train on single GPU
    python mmdetection/tools/train.py $config_path --work-dir $output_path --auto-scale-lr

    # train on multiple GPUs
    bash mmdetection/tools/dist_train.sh $config_path /*NUM_GPU*/ --work-dir $output_path --auto-scale-lr
    ```
# Run our code in mmdetection (Inference)
* Note: the default will run 1 annotation to save time. If you want to run all annotations, please set `--run_anno_id 4`.
* Inference by G-DINO only
    ```bash
        bash ./scripts/online_davis_dino_mmdet.sh ./outputs_dino --g_dino_ckpt_path ./mm_weights/groundingdino_swinb_cogcoor_mmdet-55949c9c.pth --g_dino_config_path ./mmdetection/configs/grounding_dino/grounding_dino_swin-b_rvos.py
    ```
* Inference by our trained G-DINO + SAM
    ```bash
        bash ./scripts/online_davis_sam_mmdet.sh ./outputs_gsam ../Grounded-Segment-Anything/sam_hq_vit_h.pth --g_dino_ckpt_path ./mm_weights/groundingdino_swinb_cogcoor_mmdet-55949c9c.pth --g_dino_config_path ./mmdetection/configs/grounding_dino/grounding_dino_swin-b_rvos.py
    ```


## Run our code (Training)
* Prepare data & model weight (e.g., trained G-DINO checkpoint)
* Note: num_train_steps will override the epochs setting. If you want to use epoch setting only, please set num_train_steps to -1 (i.e., the default value).
    ```bash
    bash ./scripts/online_ytvos_train_gdino.sh ./outputs --finetune_gdino_mode --batch_size 1 --epochs 1 --num_train_steps 10
    ```

## Run our code (Inference on DAVIS)
* Note: the default will run 1 annotation to save time. If you want to run all annotations, please set `--run_anno_id 4`.
* Inference by G-DINO only
    ```bash
        bash ./scripts/online_davis_dino.sh ./outputs_dino --g_dino_ckpt_path ./checkpoint.pth --use_trained_gdino
    ```
* Inference by our trained G-DINO + SAM
    ```bash
        bash ./scripts/online_davis_sam.sh ./outputs_gsam ./*SAM_checkpoint*/ --use_trained_gdino --g_dino_ckpt_path ./*GDINO_checkpoint*/ 
    ```

# SAM
## Run our code (Training)
* Prepare data & model weight (e.g., trained SAM checkpoint)
*  ```bash
    bash ./script/online_ytvos_train_sam_lora.sh ./outputs ./*SAM_checkpoint*/
    ```

## Run our code (Inference on DAVIS)
### Note: This script will perform inference and evaluation.
1. Please feed in the path of the SAM checkpoint and the path of LORA_SAM also.
2. Note that the flag of `use_LORA_SAM` need to be enabled when using LORA_SAM.
* Inference by G-SAM only
    ```bash
        bash ./scripts/online_davis_sam.sh ./outputs ./*SAM_checkpoint*/ --use_LORA_SAM --lora_sam_ckpt_path ./*LORA_SAM_checkpoint*/
    ```
* Inference by G-SAM + prop. mask with G-DINO aff. matrix
    ```bash
        bash ./scripts/online_davis_sam_prop_dino.sh ./outputs ./*SAM_checkpoint*/
    ```
* Inference by G-SAM + prop. bbox
    ```bash
        bash ./scripts/online_davis_sam_prop_dino_bbox.sh ./outputs ./*SAM_checkpoint*/
    ```
## (DO NOT USE. Still modifying, due to the somewhat different structure from DAVIS) Run our code (Inference on YTVOS)
### Note: This script will perform inference only, and the evaluation is done on the YTVOS server.
* Inference by G-SAM only (can feed in any SAM checkpoint even with our LORA tuned checkpoint)
    ```bash
        bash ./scripts/online_ytvos_sam.sh ./outputs ./*SAM_checkpoint*/
    ```
