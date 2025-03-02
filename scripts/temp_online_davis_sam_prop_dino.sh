# echo "Run on temp = 0.01"
# bash ./scripts/online_davis_sam_prop_dino.sh ./outputs_sam_prop_dino_temp_0_0_1 --visualize --prop_thres 1.0 --temp 0.01

# echo "Run on temp = 0.001"
# bash ./scripts/online_davis_sam_prop_dino.sh ./outputs_sam_prop_dino_temp_0_0_0_1 --visualize --prop_thres 1.0 --temp 0.001

# echo "Run on thres = 0.4 and temp = 0.5"
# bash ./scripts/online_davis_sam_prop_dino.sh ./outputs_sam_prop_dino_thres_0_4_temp_0_5 --visualize --prop_thres 0.4 --temp 0.5

# echo "Run on thres = 0.4 and temp = 0.1"
# bash ./scripts/online_davis_sam_prop_dino.sh ./outputs_sam_prop_dino_thres_0_4_temp_0_1 --visualize --prop_thres 0.5 --temp 0.1


echo "Run on temp = 0.5"
bash ./scripts/online_davis_sam_prop_dino_bbox.sh ./outputs_davis_sam_prop_dino_bbox_thres1_0_temp_0_5 --visualize --prop_thres 1.0 --temp 0.5

echo "Run on temp = 0.1"
bash ./scripts/online_davis_sam_prop_dino_bbox.sh ./outputs_davis_sam_prop_dino_bbox_thres1_0_temp_0_1 --visualize --prop_thres 1.0 --temp 0.1

echo "Run on temp = 0.01"
bash ./scripts/online_davis_sam_prop_dino_bbox.sh ./outputs_davis_sam_prop_dino_bbox_thres1_0_temp_0_0_1 --visualize --prop_thres 1.0 --temp 0.01

echo "Run on temp = 0.001"
bash ./scripts/online_davis_sam_prop_dino_bbox.sh ./outputs_davis_sam_prop_dino_bbox_thres1_0_temp_0_0_0_1 --visualize --prop_thres 1.0 --temp 0.001