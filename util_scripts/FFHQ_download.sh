mkdir -p $BASE_PATH/datasets/  # create datasets folder if it does not exist
cd $BASE_PATH/datasets  # move to datasets folder


# ------- FFHQ --------- #
# download ffhq from huggingface
wget https://huggingface.co/datasets/yangtao9009/FFHQ1024/resolve/main/FFHQ-1024-1.zip?download=true
wget https://huggingface.co/datasets/yangtao9009/FFHQ1024/resolve/main/FFHQ-1024-2.zip?download=true


