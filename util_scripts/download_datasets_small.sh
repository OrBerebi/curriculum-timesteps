mkdir -p $BASE_PATH/datasets/  # create datasets folder if it does not exist
cd $BASE_PATH/datasets  # move to datasets folder


# ------- LAION-10k subset --------- #
gdown --id 1lUUzhq9yK1YOWJePm12GeJgsUtCfpm_o  # images
gdown --id 1svzxtGgJRtCyWNW6pQPNGSDkhuDifTzn  # captions
gdown --id 1TAxFU_i102ZGuWm3LUrYM0nn_mX28Ab4  # precomputed LAION features
unzip train.zip # unzip LAION images
mkdir -p laion-10k # create dir
mv train/images_large/* laion-10k/ # move to appropriate folder
rm -rf train/ # clean files
rm -rf train.zip # clean files


wget https://github.com/NVlabs/edm/raw/main/dataset_tool.py
echo "Resizing LAION-10k to 128x128"
python dataset_tool.py --source=laion-10k --dest=laion-10k-resized_128 --resolution=128x128
mv laion-10k-resized_128/*/*.png laion-10k-resized_128/ # move all images to the same folder
find laion-10k-resized_128/ -mindepth 1 -type d -exec rm -r {} +  # delete empty folders
cd laion-10k-resized_128/
for f in img000*.png; do mv "$f" "${f:5}"; done  # rename files to be consistent with laion-10k.
cd ..
