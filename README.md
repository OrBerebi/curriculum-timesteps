# Curriculum-Timesteps

This project introduces a noise-aware dynamic sampling mechanism to improve training efficiency. By dynamically adapting the number of timesteps during training based on sample-specific reconstruction errors, the model enhances reconstruction quality while reducing metrics such as the Frechet Inception Distance (FID).

---

## Installation and Setup

Follow these steps to set up the project:

1. **Clone the repository**  
   ```bash
   git clone https://github.com/OrBerebi/curriculum-timesteps.git
   ```

2. **Create an Anaconda environment**  
   Create a new environment with Python 3.9:  
   ```bash
   conda create -n curriculum-timesteps-env python=3.9
   ```

3. **Activate the environment**  
   ```bash
   conda activate curriculum-timesteps-env
   ```

4. **Navigate to the project directory**  
   ```bash
   cd /path/to/curriculum-timesteps
   ```

5. **Set the `BASE_PATH`**  
   Edit the first line of the `.env` file to match the project's path:  
   ```bash
   export BASE_PATH=/path/to/curriculum-timesteps
   ```

6. **Source the `.env` file**  
   ```bash
   source .env
   ```

7. **Install dependencies**  
   ```bash
   pip install -r requirements.txt
   ```

8. **Modify the `ambient_utils` package**  
   Locate the `ambient_utils` package installed in the Anaconda environment (e.g., `/path/to/anaconda3/envs/curriculum-timesteps-env/lib/python3.9/site-packages/ambient_utils/`). Replace its contents with the files from:  
   ```bash
   $BASE_PATH/ambient_utils_modified/
   ```

---

## Running Fine-Tuning Tasks

### 1. Fine-Tuning on Stable Diffusion XL with the Base Training Model
1. Activate the Anaconda environment:  
   ```bash
   conda activate curriculum-timesteps-env
   ```
2. Navigate to the project directory:  
   ```bash
   cd $BASE_PATH
   ```
3. Source the `.env` file:  
   ```bash
   source .env
   ```
4. Launch the training script:  
   ```bash
   accelerate launch train_text_to_image_lora_sdxl_base.py --config=configs/ffhq_base.yaml
   ```

### 2. Fine-Tuning on Stable Diffusion XL with the Curriculum-Timesteps Training Model
1. Activate the Anaconda environment:  
   ```bash
   conda activate curriculum-timesteps-env
   ```
2. Navigate to the project directory:  
   ```bash
   cd $BASE_PATH
   ```
3. Source the `.env` file:  
   ```bash
   source .env
   ```
4. Launch the training script:  
   ```bash
   accelerate launch train_text_to_image_lora_sdxl_timesteps.py --config=configs/ffhq_timesteps.yaml
   ```

---

## Tracking FID or Validation During Training

To track metrics during training, update the configuration files (`.yaml`):  
- **Enable validation tracking**:  
   ```yaml
   track_val: True
   ```
- **Enable FID tracking**:  
   ```yaml
   track_fid: True
   ```

Enabling these options will check and save FID scores and verification examples during training. Note that this will noticeably increase the training time. 


