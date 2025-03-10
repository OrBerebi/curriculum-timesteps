#!/usr/bin/env python
# coding=utf-8
# Copyright 2023 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Fine-tuning script for Stable Diffusion XL for text2image with support for LoRA."""

import warnings
import argparse
import logging
import math
import os
import random
import shutil
from pathlib import Path
import yaml
import datasets
import numpy as np
import torch
import torch.nn.functional as F
import torch.utils.checkpoint
import transformers
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import DistributedDataParallelKwargs, ProjectConfiguration, set_seed
from datasets import load_dataset
from huggingface_hub import create_repo, upload_folder
from packaging import version
from peft import LoraConfig
from peft.utils import get_peft_model_state_dict
from torchvision import transforms
from torchvision.transforms.functional import crop
from tqdm.auto import tqdm
from transformers import AutoTokenizer, PretrainedConfig
import functools
import ambient_utils
import diffusers
from diffusers import (
    AutoencoderKL,
    DDPMScheduler,
    StableDiffusionXLPipeline,
    UNet2DConditionModel,
)
from diffusers.loaders import LoraLoaderMixin
from diffusers.optimization import get_scheduler
from diffusers.training_utils import cast_training_params, compute_snr
from diffusers.utils import check_min_version, convert_state_dict_to_diffusers, is_wandb_available
from diffusers.utils.import_utils import is_xformers_available
from diffusers.utils.torch_utils import is_compiled_module
import time
from ambient_utils.eval_utils import calculate_inception_stats, calculate_fid_from_inception_stats
import PIL


logger = get_logger(__name__)


def import_model_class_from_model_name_or_path(
    pretrained_model_name_or_path: str, revision: str, subfolder: str = "text_encoder"
):
    text_encoder_config = PretrainedConfig.from_pretrained(
        pretrained_model_name_or_path, subfolder=subfolder, revision=revision
    )
    model_class = text_encoder_config.architectures[0]

    if model_class == "CLIPTextModel":
        from transformers import CLIPTextModel

        return CLIPTextModel
    elif model_class == "CLIPTextModelWithProjection":
        from transformers import CLIPTextModelWithProjection

        return CLIPTextModelWithProjection
    else:
        raise ValueError(f"{model_class} is not supported.")


def parse_args(input_args=None):
    parser = argparse.ArgumentParser(description="Simple example of a training script.")
    parser.add_argument('--config_file', type=str, default=None, help='Path to config file.')
    parser.add_argument('--expr_id', type=str, default=None, help='Experiment ID.')
    parser.add_argument(
        "--pretrained_model_name_or_path",
        type=str,
        default=None,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--pretrained_vae_model_name_or_path",
        type=str,
        default=None,
        help="Path to pretrained VAE model with better numerical stability. More details: https://github.com/huggingface/diffusers/pull/4038.",
    )
    parser.add_argument(
        "--revision",
        type=str,
        default=None,
        required=False,
        help="Revision of pretrained model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--variant",
        type=str,
        default=None,
        help="Variant of the model files of the pretrained model identifier from huggingface.co/models, 'e.g.' fp16",
    )
    parser.add_argument(
        "--dataset_name",
        type=str,
        default=None,
        help=(
            "The name of the Dataset (from the HuggingFace hub) to train on (could be your own, possibly private,"
            " dataset). It can also be a path pointing to a local copy of a dataset in your filesystem,"
            " or to a folder containing files that 🤗 Datasets can understand."
        ),
    )
    parser.add_argument(
        "--dataset_config_name",
        type=str,
        default=None,
        help="The config of the Dataset, leave as None if there's only one config.",
    )
    parser.add_argument(
        "--train_data_dir",
        type=str,
        default=None,
        help=(
            "A folder containing the training data. Folder contents must follow the structure described in"
            " https://huggingface.co/docs/datasets/image_dataset#imagefolder. In particular, a `metadata.jsonl` file"
            " must exist to provide the captions for the images. Ignored if `dataset_name` is specified."
        ),
    )
    parser.add_argument(
        "--image_column", type=str, default="image", help="The column of the dataset containing an image."
    )
    parser.add_argument(
        "--caption_column",
        type=str,
        default="text",
        help="The column of the dataset containing a caption or a list of captions.",
    )
    parser.add_argument(
        "--validation_prompt",
        type=str,
        default=None,
        help="A prompt that is used during validation to verify that the model is learning.",
    )
    parser.add_argument(
        "--num_validation_images",
        type=int,
        default=4,
        help="Number of images that should be generated during validation with `validation_prompt`.",
    )
    parser.add_argument(
        "--validation_epochs",
        type=int,
        default=1,
        help=(
            "Run fine-tuning validation every X epochs. The validation process consists of running the prompt"
            " `args.validation_prompt` multiple times: `args.num_validation_images`."
        ),
    )
    parser.add_argument(
        "--validation_step_num",
        type=int,
        default=16,
        help=("Number of steps where a validaiton check will be performed."),
    )

    parser.add_argument(
        "--FID_step_num",
        type=int,
        default=16,
        help=("Number of steps where a FID check will be performed."),
    )

    parser.add_argument(
        "--skip_first_epoch",
        action="store_true",
        help="Whether or not to skip the first epoch. Useful to benchmark the first epoch.",
    )
    parser.add_argument(
        '--min_validation_steps',
        type=int,
        default=1,
        help='Minimum number of steps that need to be performed before running validation.'
    )
    parser.add_argument(
        "--max_train_samples",
        type=int,
        default=None,
        help=(
            "For debugging purposes or quicker training, truncate the number of training examples to this "
            "value if set."
        ),
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="sd-model-finetuned-lora",
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument(
        "--cache_dir",
        type=str,
        default=None,
        help="The directory where the downloaded models and datasets will be stored.",
    )
    parser.add_argument("--seed", type=int, default=None, help="A seed for reproducible training.")
    parser.add_argument(
        "--resolution",
        type=int,
        default=1024,
        help=(
            "The resolution for input images, all the images in the train/validation dataset will be resized to this"
            " resolution"
        ),
    )
    parser.add_argument(
        "--center_crop",
        default=False,
        action="store_true",
        help=(
            "Whether to center crop the input images to the resolution. If not set, the images will be randomly"
            " cropped. The images will be resized to the resolution first before cropping."
        ),
    )
    parser.add_argument(
        "--random_flip",
        action="store_true",
        help="whether to randomly flip images horizontally",
    )
    parser.add_argument(
        "--train_text_encoder",
        action="store_true",
        help="Whether to train the text encoder. If set, the text encoder should be float32 precision.",
    )
    parser.add_argument(
        "--train_batch_size", type=int, default=16, help="Batch size (per device) for the training dataloader."
    )
    parser.add_argument(
        "--eval_batch_size", type=int, default=None, help="Batch size (per device) for the evaluation dataloader."
    )
    parser.add_argument("--num_train_epochs", type=int, default=100)
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform.  If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--checkpointing_steps",
        type=int,
        default=500,
        help=(
            "Save a checkpoint of the training state every X updates. These checkpoints can be used both as final"
            " checkpoints in case they are better than the last checkpoint, and are also suitable for resuming"
            " training using `--resume_from_checkpoint`."
        ),
    )
    parser.add_argument(
        "--checkpoints_total_limit",
        type=int,
        default=None,
        help=("Max number of checkpoints to store."),
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help=(
            "Whether training should be resumed from a previous checkpoint. Use a path saved by"
            ' `--checkpointing_steps`, or `"latest"` to automatically select the last available checkpoint.'
        ),
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--gradient_checkpointing",
        action="store_true",
        help="Whether or not to use gradient checkpointing to save memory at the expense of slower backward pass.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=1e-4,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument(
        "--scale_lr",
        action="store_true",
        default=False,
        help="Scale the learning rate by the number of GPUs, gradient accumulation steps, and batch size.",
    )
    parser.add_argument(
        "--lr_scheduler",
        type=str,
        default="constant",
        help=(
            'The scheduler type to use. Choose between ["linear", "cosine", "cosine_with_restarts", "polynomial",'
            ' "constant", "constant_with_warmup"]'
        ),
    )
    parser.add_argument(
        "--lr_warmup_steps", type=int, default=500, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument(
        "--snr_gamma",
        type=float,
        default=None,
        help="SNR weighting gamma to be used if rebalancing the loss. Recommended value is 5.0. "
        "More details here: https://arxiv.org/abs/2303.09556.",
    )
    parser.add_argument(
        "--allow_tf32",
        action="store_true",
        help=(
            "Whether or not to allow TF32 on Ampere GPUs. Can be used to speed up training. For more information, see"
            " https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices"
        ),
    )
    parser.add_argument(
        "--dataloader_num_workers",
        type=int,
        default=0,
        help=(
            "Number of subprocesses to use for data loading. 0 means that the data will be loaded in the main process."
        ),
    )
    parser.add_argument(
        "--use_8bit_adam", action="store_true", help="Whether or not to use 8-bit Adam from bitsandbytes."
    )
    parser.add_argument('--with_grad', default=True, type=bool, help='Enable gradient computation')
    parser.add_argument("--adam_beta1", type=float, default=0.9, help="The beta1 parameter for the Adam optimizer.")
    parser.add_argument("--adam_beta2", type=float, default=0.999, help="The beta2 parameter for the Adam optimizer.")
    parser.add_argument("--adam_weight_decay", type=float, default=1e-2, help="Weight decay to use.")
    parser.add_argument("--adam_epsilon", type=float, default=1e-08, help="Epsilon value for the Adam optimizer")
    parser.add_argument("--max_grad_norm", default=1.0, type=float, help="Max gradient norm.")
    parser.add_argument(
        "--prediction_type",
        type=str,
        default=None,
        help="The prediction_type that shall be used for training. Choose between 'epsilon' or 'v_prediction' or leave `None`. If left to `None` the default prediction type of the scheduler: `noise_scheduler.config.prediciton_type` is chosen.",
    )
    parser.add_argument(
        "--hub_model_id",
        type=str,
        default=None,
        help="The name of the repository to keep in sync with the local `output_dir`.",
    )
    parser.add_argument(
        "--logging_dir",
        type=str,
        default="logs",
        help=(
            "[TensorBoard](https://www.tensorflow.org/tensorboard) log directory. Will default to"
            " *output_dir/runs/**CURRENT_DATETIME_HOSTNAME***."
        ),
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="tensorboard",
        help=(
            'The integration to report the results and logs to. Supported platforms are `"tensorboard"`'
            ' (default), `"wandb"` and `"comet_ml"`. Use `"all"` to report to all integrations.'
        ),
    )
    parser.add_argument(
        "--mixed_precision",
        type=str,
        default=None,
        choices=["no", "fp16", "bf16"],
        help=(
            "Whether to use mixed precision. Choose between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >="
            " 1.10.and an Nvidia Ampere GPU.  Default to the value of accelerate config of the current system or the"
            " flag passed with the `accelerate.launch` command. Use this argument to override the accelerate config."
        ),
    )
    parser.add_argument("--local_rank", type=int, default=-1, help="For distributed training: local_rank")
    parser.add_argument(
        "--enable_xformers_memory_efficient_attention", action="store_true", help="Whether or not to use xformers."
    )
    parser.add_argument("--noise_offset", type=float, default=0, help="The scale of noise offset.")
    parser.add_argument(
        "--rank",
        type=int,
        default=4,
        help=("The dimension of the LoRA update matrices."),
    )

    # Training with noisy data
    parser.add_argument("--noisy_ambient", action="store_true", help="Whether or not to train with noisy data.", default=False)
    parser.add_argument("--timestep_nature", type=int, default=100, 
                        help="If noisy_ambient is True, then the encodings will be corrupted with noise.")
    parser.add_argument("--consistency_coeff", type=float, default=0.0, help="The coefficient of the consistency loss.")
    parser.add_argument("--max_steps_diff", type=int, default=None, help="The maximum number of steps difference for consistency.")
    parser.add_argument("--num_consistency_steps", type=int, default=1, help="The number of consistency steps.")
    parser.add_argument("--x0_pred", action="store_true", help="Whether to train with x0 prediction.", default=False)

    # SLURM related
    parser.add_argument("--time_limit", type=int, default=2880, help="Time limit in minutes. Default 2 days.")

    # FID related arguments
    parser.add_argument("--track_fid", action="store_true", help="Whether or not to track FID during training.")
    parser.add_argument("--fid_ref_path", type=str, default=None, help="Path to pre-computed FID stats for the dataset.")
    parser.add_argument("--num_images_for_fid", type=int, default=100, help="Number of images for FID computation.")

    parser.add_argument("--track_val", action="store_true", help="Whether or not to track validation during training.")

    if input_args is not None:
        args = parser.parse_args(input_args)
    else:
        args = parser.parse_args()
    
    
    # load arguments from config file
    if args.config_file is not None:
        with open(args.config_file, 'r') as f:
            yaml_content = yaml.load(f, Loader=yaml.FullLoader)
            for key, value in yaml_content.items():
                if isinstance(value, str) and "$" in value:
                    yaml_content[key] = os.path.expandvars(value)            
            parser.set_defaults(**yaml_content)
        
        args = parser.parse_args()
    env_local_rank = int(os.environ.get("LOCAL_RANK", -1))
    if env_local_rank != -1 and env_local_rank != args.local_rank:
        args.local_rank = env_local_rank

    # Sanity checks
    if args.dataset_name is None and args.train_data_dir is None:
        raise ValueError("Need either a dataset name or a training folder.")

    return args


DATASET_NAME_MAPPING = {
    "lambdalabs/pokemon-blip-captions": ("image", "text"),
}


def tokenize_prompt(tokenizer, prompt):
    text_inputs = tokenizer(
        prompt,
        padding="max_length",
        max_length=tokenizer.model_max_length,
        truncation=True,
        return_tensors="pt",
    )
    text_input_ids = text_inputs.input_ids
    return text_input_ids


# Adapted from pipelines.StableDiffusionXLPipeline.encode_prompt
def encode_prompt(text_encoders, tokenizers, prompt, text_input_ids_list=None):
    prompt_embeds_list = []

    for i, text_encoder in enumerate(text_encoders):
        if tokenizers is not None:
            tokenizer = tokenizers[i]
            text_input_ids = tokenize_prompt(tokenizer, prompt)
        else:
            assert text_input_ids_list is not None
            text_input_ids = text_input_ids_list[i]

        prompt_embeds = text_encoder(
            text_input_ids.to(text_encoder.device), output_hidden_states=True, return_dict=False
        )

        # We are only ALWAYS interested in the pooled output of the final text encoder
        pooled_prompt_embeds = prompt_embeds[0]
        prompt_embeds = prompt_embeds[-1][-2]
        bs_embed, seq_len, _ = prompt_embeds.shape
        prompt_embeds = prompt_embeds.view(bs_embed, seq_len, -1)
        prompt_embeds_list.append(prompt_embeds)

    prompt_embeds = torch.concat(prompt_embeds_list, dim=-1)
    pooled_prompt_embeds = pooled_prompt_embeds.view(bs_embed, -1)
    return prompt_embeds, pooled_prompt_embeds


def compute_vae_encodings(batch, vae, noise_scheduler):
    images = batch.pop("pixel_values")
    pixel_values = torch.stack(list(images))
    pixel_values = pixel_values.to(memory_format=torch.contiguous_format).float()
    pixel_values = pixel_values.to(vae.device, dtype=vae.dtype)
    with torch.no_grad():
        model_input = vae.encode(pixel_values).latent_dist.sample()
    model_input = model_input * vae.config.scaling_factor
    timesteps = torch.ones(model_input.shape[0], device=model_input.device, dtype=torch.long) * args.timestep_nature
    if args.noisy_ambient:
        model_input = noise_scheduler.add_noise(model_input, torch.randn_like(model_input), timesteps)
    return {"model_input": model_input.cpu()}



def main(args):
    logging_dir = Path(args.output_dir, args.logging_dir)

    accelerator_project_config = ProjectConfiguration(project_dir=args.output_dir, logging_dir=logging_dir)
    kwargs = DistributedDataParallelKwargs(find_unused_parameters=True)
    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        mixed_precision=args.mixed_precision,
        log_with=args.report_to,
        project_config=accelerator_project_config,
        kwargs_handlers=[kwargs],
    )

    # start minutes timer
    start_time = time.time()
    
    if args.eval_batch_size is None:
        args.eval_batch_size = args.train_batch_size

    if args.report_to == "wandb":
        if not is_wandb_available():
            raise ImportError("Make sure to install wandb if you want to use it for logging during training.")
        import wandb

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.info(accelerator.state, main_process_only=False)
    if accelerator.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        transformers.utils.logging.set_verbosity_warning()
        diffusers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        transformers.utils.logging.set_verbosity_error()
        diffusers.utils.logging.set_verbosity_error()

    # If passed along, set the training seed now.
    if args.seed is not None:
        set_seed(args.seed)

    # Handle the repository creation
    if accelerator.is_main_process:
        if args.output_dir is not None:
            os.makedirs(args.output_dir, exist_ok=True)
    
    # set max_steps_diff to timestep_nature
    if args.max_steps_diff is None:
        args.max_steps_diff = args.timestep_nature

    # Load the tokenizers
    tokenizer_one = AutoTokenizer.from_pretrained(
        args.pretrained_model_name_or_path,
        subfolder="tokenizer",
        revision=args.revision,
        use_fast=False,
    )
    tokenizer_two = AutoTokenizer.from_pretrained(
        args.pretrained_model_name_or_path,
        subfolder="tokenizer_2",
        revision=args.revision,
        use_fast=False,
    )

    # import correct text encoder classes
    text_encoder_cls_one = import_model_class_from_model_name_or_path(
        args.pretrained_model_name_or_path, args.revision
    )
    text_encoder_cls_two = import_model_class_from_model_name_or_path(
        args.pretrained_model_name_or_path, args.revision, subfolder="text_encoder_2"
    )

    # Load scheduler and models
    noise_scheduler = DDPMScheduler.from_pretrained(args.pretrained_model_name_or_path, subfolder="scheduler")
    text_encoder_one = text_encoder_cls_one.from_pretrained(
        args.pretrained_model_name_or_path, subfolder="text_encoder", revision=args.revision, variant=args.variant
    )
    text_encoder_two = text_encoder_cls_two.from_pretrained(
        args.pretrained_model_name_or_path, subfolder="text_encoder_2", revision=args.revision, variant=args.variant
    )
    vae_path = (
        args.pretrained_model_name_or_path
        if args.pretrained_vae_model_name_or_path is None
        else args.pretrained_vae_model_name_or_path
    )
    vae = AutoencoderKL.from_pretrained(
        vae_path,
        subfolder="vae" if args.pretrained_vae_model_name_or_path is None else None,
        revision=args.revision,
        variant=args.variant,
    )
    unet = UNet2DConditionModel.from_pretrained(
        args.pretrained_model_name_or_path, subfolder="unet", revision=args.revision, variant=args.variant
    )

    # We only train the additional adapter LoRA layers
    vae.requires_grad_(False)
    text_encoder_one.requires_grad_(False)
    text_encoder_two.requires_grad_(False)
    unet.requires_grad_(False)

    # For mixed precision training we cast all non-trainable weigths (vae, non-lora text_encoder and non-lora unet) to half-precision
    # as these weights are only used for inference, keeping weights in full precision is not required.
    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16
    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    # Move unet, vae and text_encoder to device and cast to weight_dtype
    # The VAE is in float32 to avoid NaN losses.
    unet.to(accelerator.device, dtype=weight_dtype)

    if args.pretrained_vae_model_name_or_path is None:
        vae.to(accelerator.device, dtype=torch.float32)
    else:
        vae.to(accelerator.device, dtype=weight_dtype)
    text_encoder_one.to(accelerator.device, dtype=weight_dtype)
    text_encoder_two.to(accelerator.device, dtype=weight_dtype)

    if args.enable_xformers_memory_efficient_attention:
        if is_xformers_available():
            import xformers

            xformers_version = version.parse(xformers.__version__)
            if xformers_version == version.parse("0.0.16"):
                logger.warn(
                    "xFormers 0.0.16 cannot be used for training in some GPUs. If you observe problems during training, please update xFormers to at least 0.0.17. See https://huggingface.co/docs/diffusers/main/en/optimization/xformers for more details."
                )
            unet.enable_xformers_memory_efficient_attention()
        else:
            raise ValueError("xformers is not available. Make sure it is installed correctly")

    # now we will add new LoRA weights to the attention layers
    # Set correct lora layers
    unet_lora_config = LoraConfig(
        r=args.rank,
        lora_alpha=args.rank,
        init_lora_weights="gaussian",
        target_modules=["to_k", "to_q", "to_v", "to_out.0"],
    )
    print("Adding adapter...")
    unet.add_adapter(unet_lora_config)
    unet.to(accelerator.device, dtype=weight_dtype)

    # The text encoder comes from 🤗 transformers, we will also attach adapters to it.
    if args.train_text_encoder:
        # ensure that dtype is float32, even if rest of the model that isn't trained is loaded in fp16
        text_lora_config = LoraConfig(
            r=args.rank,
            lora_alpha=args.rank,
            init_lora_weights="gaussian",
            target_modules=["q_proj", "k_proj", "v_proj", "out_proj"],
        )
        text_encoder_one.add_adapter(text_lora_config)
        text_encoder_two.add_adapter(text_lora_config)

    # Make sure the trainable params are in float32.
    if args.mixed_precision == "fp16":
        models = [unet]
        if args.train_text_encoder:
            models.extend([text_encoder_one, text_encoder_two])
        # only upcast trainable parameters (LoRA) into fp32
        cast_training_params(models, dtype=torch.float32)
    

    def unwrap_model(model):
        model = accelerator.unwrap_model(model)
        model = model._orig_mod if is_compiled_module(model) else model
        return model

    # create custom saving & loading hooks so that `accelerator.save_state(...)` serializes in a nice format
    def save_model_hook(models, weights, output_dir):
        if accelerator.is_main_process:
            # there are only two options here. Either are just the unet attn processor layers
            # or there are the unet and text encoder atten layers
            unet_lora_layers_to_save = None
            text_encoder_one_lora_layers_to_save = None
            text_encoder_two_lora_layers_to_save = None

            for model in models:
                if isinstance(model, type(unwrap_model(unet))):
                    unet_lora_layers_to_save = convert_state_dict_to_diffusers(get_peft_model_state_dict(model))
                elif isinstance(model, type(unwrap_model(text_encoder_one))):
                    text_encoder_one_lora_layers_to_save = convert_state_dict_to_diffusers(
                        get_peft_model_state_dict(model)
                    )
                elif isinstance(model, type(unwrap_model(text_encoder_two))):
                    text_encoder_two_lora_layers_to_save = convert_state_dict_to_diffusers(
                        get_peft_model_state_dict(model)
                    )
                else:
                    raise ValueError(f"unexpected save model: {model.__class__}")

                # make sure to pop weight so that corresponding model is not saved again
                weights.pop()
            
            StableDiffusionXLPipeline.save_lora_weights(
                output_dir,
                unet_lora_layers=unet_lora_layers_to_save,
                text_encoder_lora_layers=text_encoder_one_lora_layers_to_save,
                text_encoder_2_lora_layers=text_encoder_two_lora_layers_to_save,
            )

    def load_model_hook(models, input_dir):
        unet_ = None
        text_encoder_one_ = None
        text_encoder_two_ = None

        while len(models) > 0:
            model = models.pop()

            if isinstance(model, type(unwrap_model(unet))):
                unet_ = model
            elif isinstance(model, type(unwrap_model(text_encoder_one))):
                text_encoder_one_ = model
            elif isinstance(model, type(unwrap_model(text_encoder_two))):
                text_encoder_two_ = model
            else:
                raise ValueError(f"unexpected save model: {model.__class__}")
    
        unet_params = list(filter(lambda p: p[1].requires_grad, unet_.named_parameters()))
        
        lora_state_dict, network_alphas = LoraLoaderMixin.lora_state_dict(input_dir)
        # hacky way to load unet params
        for (param_name, param_value) in unet_params:
            # keys = list(lora_state_dict.keys())
            mapped_name = ("unet." + param_name).replace("lora_A.default", "lora.down").replace("lora_B.default", "lora.up")
            new_param_value = lora_state_dict[mapped_name]
            param_value.data = new_param_value.to(param_value.data.device).to(param_value.data.dtype)
        # LoraLoaderMixin.load_lora_into_unet(lora_state_dict, network_alphas=network_alphas, unet=unet_)

        text_encoder_state_dict = {k: v for k, v in lora_state_dict.items() if "text_encoder." in k}
        LoraLoaderMixin.load_lora_into_text_encoder(
            text_encoder_state_dict, network_alphas=network_alphas, text_encoder=text_encoder_one_
        )

        text_encoder_2_state_dict = {k: v for k, v in lora_state_dict.items() if "text_encoder_2." in k}
        LoraLoaderMixin.load_lora_into_text_encoder(
            text_encoder_2_state_dict, network_alphas=network_alphas, text_encoder=text_encoder_two_
        )

    accelerator.register_save_state_pre_hook(save_model_hook)
    accelerator.register_load_state_pre_hook(load_model_hook)

    if args.gradient_checkpointing:
        unet.enable_gradient_checkpointing()
        if args.train_text_encoder:
            text_encoder_one.gradient_checkpointing_enable()
            text_encoder_two.gradient_checkpointing_enable()

    # Enable TF32 for faster training on Ampere GPUs,
    # cf https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices
    if args.allow_tf32:
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True


    if args.scale_lr:
        args.learning_rate = (
            args.learning_rate * args.gradient_accumulation_steps * args.train_batch_size * accelerator.num_processes
        )

    # Use 8-bit Adam for lower memory usage or to fine-tune the model in 16GB GPUs
    if args.use_8bit_adam:
        try:
            import bitsandbytes as bnb
        except ImportError:
            raise ImportError(
                "To use 8-bit Adam, please install the bitsandbytes library: `pip install bitsandbytes`."
            )

        optimizer_class = bnb.optim.AdamW8bit
    else:
        optimizer_class = torch.optim.AdamW

    # Optimizer creation
    params_to_optimize = list(filter(lambda p: p.requires_grad, unet.parameters()))
    if args.train_text_encoder:
        params_to_optimize = (
            params_to_optimize
            + list(filter(lambda p: p.requires_grad, text_encoder_one.parameters()))
            + list(filter(lambda p: p.requires_grad, text_encoder_two.parameters()))
        )
    optimizer = optimizer_class(
        params_to_optimize,
        lr=args.learning_rate,
        betas=(args.adam_beta1, args.adam_beta2),
        weight_decay=args.adam_weight_decay,
        eps=args.adam_epsilon,
    )

    # Get the datasets: you can either provide your own training and evaluation files (see below)
    # or specify a Dataset from the hub (the dataset will be downloaded automatically from the datasets Hub).

    # In distributed training, the load_dataset function guarantees that only one local process can concurrently
    # download the dataset.
    if args.dataset_name is not None:
        # Downloading and loading a dataset from the hub.
        dataset = load_dataset(
            args.dataset_name, args.dataset_config_name, cache_dir=args.cache_dir, data_dir=args.train_data_dir
        )
    else:
        data_files = {}
        if args.train_data_dir is not None:
            data_files["train"] = os.path.join(args.train_data_dir, "**")
        dataset = load_dataset(
            "imagefolder",
            data_files=data_files,
            cache_dir=args.cache_dir,
        )
        # See more about loading custom images at
        # https://huggingface.co/docs/datasets/v2.4.0/en/image_load#imagefolder

    # Preprocessing the datasets.
    # We need to tokenize inputs and targets.
    column_names = dataset["train"].column_names

    # 6. Get the column names for input/target.
    dataset_columns = DATASET_NAME_MAPPING.get(args.dataset_name, None)
    if args.image_column is None:
        image_column = dataset_columns[0] if dataset_columns is not None else column_names[0]
    else:
        image_column = args.image_column
        if image_column not in column_names:
            raise ValueError(
                f"--image_column' value '{args.image_column}' needs to be one of: {', '.join(column_names)}"
            )
    if args.caption_column is None:
        caption_column = dataset_columns[1] if dataset_columns is not None else column_names[1]
    else:
        caption_column = args.caption_column
        if caption_column not in column_names:
            # warning that there is no caption in the dataset
            logger.warning(
                f"--caption_column' value '{args.caption_column}' not found in column_names: {', '.join(column_names)}")

    # Preprocessing the datasets.
    # We need to tokenize input captions and transform the images.
    def tokenize_captions(examples, is_train=True):
        captions = []
        if caption_column in examples:
            for caption in examples[caption_column]:
                if isinstance(caption, str):
                    captions.append(caption)
                elif isinstance(caption, (list, np.ndarray)):
                    # take a random caption if there are multiple
                    captions.append(random.choice(caption) if is_train else caption[0])
                else:
                    raise ValueError(
                        f"Caption column `{caption_column}` should contain either strings or lists of strings."
                    )
        else:
            captions = [args.validation_prompt] * len(examples)
        tokens_one = tokenize_prompt(tokenizer_one, captions)
        tokens_two = tokenize_prompt(tokenizer_two, captions)
        return tokens_one, tokens_two

    # Preprocessing the datasets.
    train_resize = transforms.Resize(args.resolution, interpolation=transforms.InterpolationMode.BILINEAR)
    train_crop = transforms.CenterCrop(args.resolution) if args.center_crop else transforms.RandomCrop(args.resolution)
    train_flip = transforms.RandomHorizontalFlip(p=1.0)
    train_transforms = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize([0.5], [0.5]),
        ]
    )

    def preprocess_train(examples):
        images = [image.convert("RGB") for image in examples[image_column]]
        # image aug
        original_sizes = []
        all_images = []
        crop_top_lefts = []
        for image in images:
            original_sizes.append((image.height, image.width))
            image = train_resize(image)
            if args.random_flip and random.random() < 0.5:
                # flip
                image = train_flip(image)
            if args.center_crop:
                y1 = max(0, int(round((image.height - args.resolution) / 2.0)))
                x1 = max(0, int(round((image.width - args.resolution) / 2.0)))
                image = train_crop(image)
            else:
                y1, x1, h, w = train_crop.get_params(image, (args.resolution, args.resolution))
                image = crop(image, y1, x1, h, w)
            crop_top_left = (y1, x1)
            crop_top_lefts.append(crop_top_left)
            image = train_transforms(image)
            all_images.append(image)

        examples["original_sizes"] = original_sizes
        examples["crop_top_lefts"] = crop_top_lefts
        examples["pixel_values"] = all_images
        tokens_one, tokens_two = tokenize_captions(examples)
        examples["input_ids_one"] = tokens_one
        examples["input_ids_two"] = tokens_two
        return examples

    with accelerator.main_process_first():
        if args.max_train_samples is not None:
            dataset["train"] = dataset["train"].shuffle(seed=args.seed).select(range(args.max_train_samples))
        # Set the training transforms
        train_dataset = dataset["train"].with_transform(preprocess_train)

    compute_vae_encodings_fn = functools.partial(compute_vae_encodings, vae=vae, noise_scheduler=noise_scheduler)
    logger.info("⏳ Computing vae embeddings", main_process_only=True)
    with accelerator.main_process_first():
        from datasets.fingerprint import Hasher

        # fingerprint used by the cache for the other processes to load the result
        # details: https://github.com/huggingface/diffusers/pull/4038#discussion_r1266078401
        new_fingerprint_for_vae = Hasher.hash(f"vae_noisy_ambient_{args.noisy_ambient}_timestep_nature_{args.timestep_nature}_total_samples_{len(train_dataset)}_resolution_{args.resolution}")
        
        train_dataset = train_dataset.map(
            compute_vae_encodings_fn,
            batched=True,
            batch_size=args.train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps,
            new_fingerprint=new_fingerprint_for_vae,
        )
        logging.info("We have mapped the VAE encodings.")



    def collate_fn(examples):
        model_input = torch.stack([torch.tensor(example["model_input"]) for example in examples])
        original_sizes = [example["original_sizes"] for example in examples]
        crop_top_lefts = [example["crop_top_lefts"] for example in examples]
        input_ids_one = torch.stack([example["input_ids_one"] for example in examples])
        input_ids_two = torch.stack([example["input_ids_two"] for example in examples])
        return {
            "model_input": model_input,
            "input_ids_one": input_ids_one,
            "input_ids_two": input_ids_two,
            "original_sizes": original_sizes,
            "crop_top_lefts": crop_top_lefts,
        }

    assert len(train_dataset) > 0, "Dataset did not load correctly. Assert path to the dataset is set correctly."
    logging.info("Initializing dataloader...")
    # DataLoaders creation:
    train_dataloader = torch.utils.data.DataLoader(train_dataset, shuffle=True, collate_fn=collate_fn, batch_size=args.train_batch_size, num_workers=args.dataloader_num_workers)
    logging.info("Dataloader initialized...")

    # Scheduler and math around the number of training steps.
    overrode_max_train_steps = False
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True

    lr_scheduler = get_scheduler(
        args.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=args.lr_warmup_steps * args.gradient_accumulation_steps,
        num_training_steps=args.max_train_steps * args.gradient_accumulation_steps,
    )
    
    logging.info("Preparing models...")
    # Prepare everything with our `accelerator`.
    if args.train_text_encoder:
        unet, text_encoder_one, text_encoder_two, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
            unet, text_encoder_one, text_encoder_two, optimizer, train_dataloader, lr_scheduler
        )
    else:
        unet, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
            unet, optimizer, train_dataloader, lr_scheduler
        )
    logging.info("Models prepared!")
    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    # Afterwards we recalculate our number of training epochs
    args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if accelerator.is_main_process:
        accelerator.init_trackers("text2image-fine-tune", config=vars(args), init_kwargs={"wandb": {"name": args.expr_id}})

    # Train!
    total_batch_size = args.train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")
    global_step = 0
    first_epoch = 0
    last_validation_step = -math.inf

    # Potentially load in the weights and states from a previous save
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint != "latest":
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = os.listdir(args.output_dir)
            dirs = [d for d in dirs if d.startswith("checkpoint")]
            dirs = sorted(dirs, key=lambda x: int(x.split("-")[1]))
            path = dirs[-1] if len(dirs) > 0 else None

        if path is None:
            accelerator.print(
                f"Checkpoint '{args.resume_from_checkpoint}' does not exist. Starting a new training run."
            )
            args.resume_from_checkpoint = None
            initial_global_step = 0
        else:
            accelerator.print(f"Resuming from checkpoint {path}")
            accelerator.load_state(os.path.join(args.output_dir, path))
            accelerator.print(f"Loaded checkpoint {path}")
            global_step = int(path.split("-")[1])

            initial_global_step = global_step
            first_epoch = global_step // num_update_steps_per_epoch

    else:
        initial_global_step = 0

    progress_bar = tqdm(
        range(0, args.max_train_steps),
        initial=initial_global_step,
        desc="Steps",
        # Only show the progress bar once on each machine.
        disable=not accelerator.is_local_main_process,
    )

    if args.noisy_ambient:
        pipe_stop_index = args.timestep_nature
    else:
        pipe_stop_index = None

    def gen_images(pipe, pipe_stop_index, output_type="pil", log_under="test", timesteps=None, 
                   num_validation_images=None, track_images=True, distributed=True):
        if num_validation_images is None:
            num_validation_images = args.num_validation_images
        # run inference
        images = []
        if args.validation_prompt and num_validation_images > 0:
            generator = torch.Generator(device=accelerator.device).manual_seed(args.seed * (1 + ambient_utils.dist.get_rank())) if args.seed else None

            #with torch.cuda.amp.autocast():
            #with torch.amp.autocast('cuda'):
            with torch.amp.autocast(device_type=pipe.device.type):
                os.makedirs(os.path.join(args.output_dir, "validation_images"), exist_ok=True)
                os.makedirs(os.path.join(args.output_dir, "validation_images", str(global_step)), exist_ok=True)
                if distributed:
                    rank_batches = ambient_utils.dist.get_rank_batches(num_validation_images, 1)
                else:
                    rank_batches = torch.arange(num_validation_images).tensor_split(num_validation_images)
                for batch_index in rank_batches:
                    if len(batch_index) == 0:
                        break

                    denoising_end = 1. - pipe_stop_index / pipe.scheduler.config.num_train_timesteps if pipe_stop_index is not None else 1.0
                    pipe_kwargs = {
                        "generator": generator,
                        "num_inference_steps": 25,
                        "prompts": [args.validation_prompt] * len(batch_index),

                    }
                    image_tensor = ambient_utils.diffusers_utils.sample_with_early_stop(pipe, denoising_end, **pipe_kwargs)
                    image_path = os.path.join(args.output_dir, "validation_images", str(global_step), f"{str(int(batch_index)).zfill(6)}.png")
                    ambient_utils.save_images(image_tensor, image_path)
                    images.append(PIL.Image.open(image_path))

            if not track_images:
                return
            for tracker in accelerator.trackers:    
                if tracker.name == "tensorboard":
                    np_images = np.stack([np.asarray(img) for img in images])
                    tracker.writer.add_images(log_under, np_images, epoch, dataformats="NHWC")
                if tracker.name == "wandb":
                    tracker.log(
                        {
                            log_under: [
                                wandb.Image(image, caption=f"{i}: {args.validation_prompt}")
                                for i, image in enumerate(images)
                            ]
                        }
                    )
        del images
        # return images

    #default_noise_gain = 1.0  # Default noise gain for the first iteration
    previous_loss = None      # Placeholder for loss from the previous iteration
    # Define epsilon, a small constant to prevent zero noise gain
    #epsilon_sigma = 1e-6
    

    def adjust_timesteps(initial_timesteps, loss, min_timesteps, max_timesteps, global_step, max_steps):
        """
        Adjust timesteps dynamically based on loss value, incorporating curriculum learning.

        Args:
            initial_timesteps (torch.Tensor): Original timesteps, shape (batch_size).
            loss (torch.Tensor): Loss values, shape (batch_size), range ~0.01-0.5.
            min_timesteps (int): Minimum allowable timesteps.
            max_timesteps (int): Maximum allowable timesteps.
            global_step (int): Current training step.
            max_steps (int): Total number of training steps.

        Returns:
            torch.Tensor: Adjusted timesteps, shape (batch_size).
        """
        # Check inputs
        #assert torch.all(loss >= 0.01) and torch.all(loss <= 0.5), f"Loss values out of range: {loss}"
        #assert global_step >= 0 and global_step <= max_steps, f"Global step out of range: {global_step}"
        
        if torch.any(loss < 0.01) or torch.any(loss > 0.5):
            loss = torch.clamp(loss, 0.01, 0.5)

        

        # Check normalization
        loss_normalized = (loss - 0.01) / (0.5 - 0.01)
        loss_normalized = torch.clamp(loss_normalized, 0.0, 1.0)

        # Check curriculum factor
        curriculum_factor = min(1.0, global_step / (0.2 * max_steps))

        # Check adjustment factor
        non_linear_factor = torch.sigmoid((loss_normalized - 0.5) * 10)
        adjustment_factor = (1 - 2 * loss_normalized) * (1 - curriculum_factor) + non_linear_factor * curriculum_factor

        # Check adjustment
        adjustment = adjustment_factor * (max_timesteps - min_timesteps) * 0.5 * curriculum_factor

        # Ensure clamped timesteps
        adjusted_timesteps = torch.clamp((initial_timesteps + adjustment).long(), min=min_timesteps, max=max_timesteps)



        # Clamp to valid range for each sample
        return adjusted_timesteps





    for epoch in range(first_epoch, args.num_train_epochs):
        unet.train()
        if args.train_text_encoder:
            text_encoder_one.train()
            text_encoder_two.train()
        train_loss = 0.0
        consistency_train_loss = 0.0

        for step, batch in enumerate(train_dataloader):
            # check if we are 10mins before the time limit
            if time.time() - start_time > (args.time_limit - 10) * 60:
                logger.info("Time limit reached. Exiting.")
                import sys; sys.exit(0)

            with accelerator.accumulate(unet):
                model_input = batch["model_input"].to(accelerator.device, dtype=weight_dtype)
                # Sample noise that we'll add to the latents
                noise = torch.randn_like(model_input)
                if args.noise_offset:
                    # https://www.crosslabs.org//blog/diffusion-with-offset-noise
                    noise += args.noise_offset * torch.randn(
                        (model_input.shape[0], model_input.shape[1], 1, 1), device=model_input.device
                    )
                

                """
                args.timestep_nature = adjust_timesteps(loss = previous_loss, min_timesteps=10, max_timesteps=100)
                # ========= NOISE MODIFICATION ==========
                if previous_loss is None:
                    # First iteration: Use default noise gain
                    #accelerator.print("First iteration: No noise gain")
                    noise_gain  = torch.tensor(default_noise_gain, device=model_input.device)
                else:

                    # Compute noise gain based on previous loss
                    loss_sigmoid = torch.sigmoid(previous_loss.detach())  # Detach to avoid backprop issues
                    noise_gain   = 1 - loss_sigmoid + epsilon_sigma
                    #accelerator.print(f"\n\n noise gain = {noise_gain}")

                # Scale the noise by the noise gain
                adjusted_noise = noise * noise_gain

                
                # ==========END: NOISE MODIFICATION ==========
                """

                bsz = model_input.shape[0]
                # Sample a random timestep for each image
                timesteps = torch.randint(args.timestep_nature, noise_scheduler.config.num_train_timesteps, (bsz,), device=model_input.device)
                timesteps = timesteps.long()

                # Add noise to the model input according to the noise magnitude at each timestep
                # (this is the forward diffusion process)
                if args.noisy_ambient:
                    desired_sigmas, noise_gain_desired = ambient_utils.diffusers_utils.timesteps_to_sigma(timesteps, noise_scheduler.alphas_cumprod.to(timesteps.device))
                    current_sigmas, noise_gain_current = ambient_utils.diffusers_utils.timesteps_to_sigma(torch.ones_like(timesteps) * args.timestep_nature, noise_scheduler.alphas_cumprod.to(timesteps.device))

                    #desired_sigmas, noise_gain_desired = ambient_utils.diffusers_utils.timesteps_to_sigma(timesteps, noise_scheduler.alphas_cumprod.to(timesteps.device), loss=previous_loss)
                    #current_sigmas, noise_gain_current = ambient_utils.diffusers_utils.timesteps_to_sigma(torch.ones_like(timesteps) * args.timestep_nature, noise_scheduler.alphas_cumprod.to(timesteps.device), loss=previous_loss)

                    #desired_sigmas = ambient_utils.diffusers_utils.timesteps_to_sigma(timesteps, noise_scheduler.alphas_cumprod.to(timesteps.device))
                    #current_sigmas = ambient_utils.diffusers_utils.timesteps_to_sigma(torch.ones_like(timesteps) * args.timestep_nature, noise_scheduler.alphas_cumprod.to(timesteps.device))                    
                    noisy_model_input, noise_realization, noise_mask = ambient_utils.add_extra_noise_from_vp_to_vp(model_input, current_sigmas, desired_sigmas)
                else:
                    noisy_model_input = noise_scheduler.add_noise(model_input, adjusted_noise, timesteps)
                    noise_mask = torch.ones(noisy_model_input.shape[0], device=noisy_model_input.device, dtype=torch.long)

                # time ids
                def compute_time_ids(original_size, crops_coords_top_left):
                    # Adapted from pipeline.StableDiffusionXLPipeline._get_add_time_ids
                    target_size = (args.resolution, args.resolution)
                    add_time_ids = list(original_size + crops_coords_top_left + target_size)
                    add_time_ids = torch.tensor([add_time_ids])
                    add_time_ids = add_time_ids.to(accelerator.device, dtype=weight_dtype)
                    return add_time_ids

                add_time_ids = torch.cat(
                    [compute_time_ids(s, c) for s, c in zip(batch["original_sizes"], batch["crop_top_lefts"])]
                )

                # Predict the noise residual
                unet_added_conditions = {"time_ids": add_time_ids}
                prompt_embeds, pooled_prompt_embeds = encode_prompt(
                    text_encoders=[text_encoder_one, text_encoder_two],
                    tokenizers=None,
                    prompt=None,
                    text_input_ids_list=[batch["input_ids_one"], batch["input_ids_two"]],
                )
                unet_added_conditions.update({"text_embeds": pooled_prompt_embeds})
                model_pred = unet(
                    noisy_model_input,
                    timesteps,
                    prompt_embeds,
                    added_cond_kwargs=unet_added_conditions,
                    return_dict=False,
                )[0]

                # Get the target for loss depending on the prediction type
                if args.prediction_type is not None:
                    # set prediction_type of scheduler if defined
                    noise_scheduler.register_to_config(prediction_type=args.prediction_type)
                
                if not args.noisy_ambient:
                    if noise_scheduler.config.prediction_type == "epsilon":
                        target = noise
                    elif noise_scheduler.config.prediction_type == "v_prediction":
                        target = noise_scheduler.get_velocity(model_input, noise, timesteps)
                    else:
                        raise ValueError(f"Unknown prediction type {noise_scheduler.config.prediction_type}")
                else:
                    assert noise_scheduler.config.prediction_type == "epsilon", "Only epsilon prediction type is supported for noisy ambient"
                    # the model outputs the noise to the clean image. We can use that to predict the clean image itself.
                    x0_pred = ambient_utils.from_noise_pred_to_x0_pred_vp(noisy_model_input, model_pred, desired_sigmas)
                    xn_pred = ambient_utils.from_x0_pred_to_xnature_pred_vp_to_vp(x0_pred, noisy_model_input, current_sigmas, desired_sigmas)

                    if args.x0_pred:
                        model_pred = xn_pred
                        target = model_input
                    else:
                        xtn_coeff = torch.sqrt((1 - desired_sigmas ** 2) / (1 - current_sigmas ** 2))
                        noise_coeff = torch.sqrt((desired_sigmas ** 2 - current_sigmas ** 2) / (1 - current_sigmas ** 2))
                        noise_pred = (noisy_model_input - xtn_coeff[:, None, None, None] * xn_pred) / noise_coeff[:, None, None, None]
                        model_pred = noise_pred
                        target = noise_realization

                if args.snr_gamma is None:
                    # This is what we are using
                    loss = (model_pred.float() - target.float()) ** 2                    
                    loss = torch.where(noise_mask[:, None, None, None] == 0, torch.zeros_like(loss), loss)
                    loss = ambient_utils.get_mean_loss(loss, noise_mask)
                    

                else:
                    assert not args.noisy_ambient, "SNR weighting is not supported for noisy ambient"
                    # Compute loss-weights as per Section 3.4 of https://arxiv.org/abs/2303.09556.
                    # Since we predict the noise instead of x_0, the original formulation is slightly changed.
                    # This is discussed in Section 4.2 of the same paper.
                    snr = compute_snr(noise_scheduler, timesteps)
                    if noise_scheduler.config.prediction_type == "v_prediction":
                        # Velocity objective requires that we add one to SNR values before we divide by them.
                        snr = snr + 1
                    mse_loss_weights = (
                        torch.stack([snr, args.snr_gamma * torch.ones_like(timesteps)], dim=1).min(dim=1)[0] / snr
                    )

                    loss = F.mse_loss(model_pred.float(), target.float(), reduction="none")
                    loss = loss.mean(dim=list(range(1, len(loss.shape)))) * mse_loss_weights
                
                """
                # ==========================================================
                # ==============MODIFICATIONS START HERE====================
                # ==========================================================
                
                # Adjust the timesteps
                adjusted_timestep_nature = adjust_timesteps(initial_timesteps = args.timestep_nature, loss = loss_per_sample, min_timesteps=1, max_timesteps=500, global_step = global_step, max_steps = args.max_train_steps  )

                #adjusted_timesteps = adjust_timesteps(initial_timesteps = timesteps, loss = loss_per_sample, min_timesteps=args.timestep_nature, max_timesteps=noise_scheduler.config.num_train_timesteps)
                #accelerator.print(f"\n\nadjusted_timesteps: {adjusted_timesteps}...")
                #accelerator.print(f"\ntimesteps: {timesteps}...")
                #accelerator.print(f"\nloss_per_sample: {loss_per_sample}...")
                # =======================================================
                # Second loss calculation based on the ajusted timesteps
                # =======================================================
                desired_sigmas, noise_gain_desired = ambient_utils.diffusers_utils.timesteps_to_sigma(timesteps, noise_scheduler.alphas_cumprod.to(model_input.device))
                current_sigmas, noise_gain_current = ambient_utils.diffusers_utils.timesteps_to_sigma(torch.ones_like(timesteps) * adjusted_timestep_nature, noise_scheduler.alphas_cumprod.to(model_input.device))
                noisy_model_input, noise_realization, noise_mask = ambient_utils.add_extra_noise_from_vp_to_vp(model_input, current_sigmas, desired_sigmas)
                model_pred = unet(
                    noisy_model_input,
                    timesteps,
                    prompt_embeds,
                    added_cond_kwargs=unet_added_conditions,
                    return_dict=False,
                )[0]

                x0_pred = ambient_utils.from_noise_pred_to_x0_pred_vp(noisy_model_input, model_pred, desired_sigmas)
                xn_pred = ambient_utils.from_x0_pred_to_xnature_pred_vp_to_vp(x0_pred, noisy_model_input, current_sigmas, desired_sigmas)
                model_pred = xn_pred
                target = model_input
                loss = (model_pred.float() - target.float()) ** 2                    
                loss = torch.where(noise_mask[:, None, None, None] == 0, torch.zeros_like(loss), loss)
                loss = ambient_utils.get_mean_loss(loss, noise_mask)

                
                # ==========================================================
                # ==============MODIFICATIONS END HERE======================
                # ==========================================================
                """



                if args.consistency_coeff > 0.0:
                    if args.run_consistency_everywhere:
                        xt = noisy_model_input
                        timesteps_xt = timesteps
                    else:
                        # compute values at the boundary
                        xt = model_input
                        timesteps_xt = torch.ones_like(timesteps) * args.timestep_nature


                    def move_one_step(xt, timesteps_xt, timesteps_xs=None):
                        sigma_t = ambient_utils.diffusers_utils.timesteps_to_sigma(timesteps_xt, noise_scheduler.alphas_cumprod.to(timesteps.device))
                        var_t = sigma_t ** 2
                        noise_pred_xt = unet(xt, timesteps_xt, prompt_embeds, added_cond_kwargs=unet_added_conditions, return_dict=False)[0]
                        x0_pred = ambient_utils.from_noise_pred_to_x0_pred_vp(xt, noise_pred_xt, sigma_t)

                        if timesteps_xs is None:
                            # select different timesteps in [timesteps_xt - args.max_steps_diff, timesteps_xt]
                            steps_diffs = torch.randint(1, args.max_steps_diff + 1, (bsz,), device=timesteps_xt.device)
                            timesteps_xs = torch.max(torch.zeros_like(timesteps_xt), timesteps_xt - steps_diffs)

                        sigma_s = ambient_utils.diffusers_utils.timesteps_to_sigma(timesteps_xs, noise_scheduler.alphas_cumprod.to(timesteps_xt.device))
                        var_s = sigma_s ** 2
                        alpha_s = torch.sqrt(1 - var_s)[:, None, None, None]

                        # Move there two times with (1 step) stochastic sampler
                        fresh_noise_coeff = ((var_s / var_t).sqrt() * (1 - (1 - var_t) / (1  - var_s)).sqrt())[:, None, None, None]
                        old_noise_coeff = torch.max(var_s[:, None, None, None] - fresh_noise_coeff ** 2, torch.zeros_like(fresh_noise_coeff)).sqrt()
                        
                        return_dict = {
                            "xs": alpha_s * x0_pred + old_noise_coeff * noise_pred_xt + fresh_noise_coeff * torch.randn_like(x0_pred),
                            "xs_alt": alpha_s * x0_pred + old_noise_coeff * noise_pred_xt + fresh_noise_coeff * torch.randn_like(x0_pred),
                            "timesteps_xs": timesteps_xs,
                            "x0_pred": x0_pred,
                        }
                        return return_dict
                    
                    with torch.set_grad_enabled(args.with_grad):
                        x_curr = torch.clone(xt)
                        t_curr = torch.clone(timesteps_xt)
                        used_timesteps = []
                        # move first iterate                        
                        for i in range(args.num_consistency_steps):
                            return_dict = move_one_step(x_curr, t_curr)
                            if i == 0:
                                # keep prediction at the boundary
                                x0_pred = return_dict["x0_pred"]
                            t_curr = return_dict["timesteps_xs"]
                            used_timesteps.append(t_curr)
                            x_curr = return_dict["xs"]
                        
                        x_t_prime_1 = x_curr
                        if args.num_consistency_steps == 1:
                            # save time and memory
                            x_curr = return_dict["xs_alt"]
                            t_curr = return_dict["timesteps_xs"]
                        else:
                            x_curr = torch.clone(xt)
                            t_curr = torch.clone(timesteps_xt)
                            # move second iterate
                            for i in range(args.num_consistency_steps):
                                return_dict = move_one_step(x_curr, t_curr, used_timesteps[i])
                                t_curr = return_dict["timesteps_xs"]
                                x_curr = return_dict["xs"]

                    x_t_prime_2 = x_curr
                    timesteps_xs = t_curr                            
                    sigma_s = ambient_utils.diffusers_utils.timesteps_to_sigma(timesteps_xs, noise_scheduler.alphas_cumprod.to(timesteps.device))
                    
                    # Predict at the new timesteps
                    preds_prime_1 = unet(x_t_prime_1, timesteps_xs, prompt_embeds, added_cond_kwargs=unet_added_conditions, return_dict=False)[0]
                    preds_prime_2 = unet(x_t_prime_2, timesteps_xs, prompt_embeds, added_cond_kwargs=unet_added_conditions, return_dict=False)[0]

                    # from noise_pred to x0_pred
                    preds_prime_1 = ambient_utils.from_noise_pred_to_x0_pred_vp(x_t_prime_1, preds_prime_1, sigma_s)
                    preds_prime_2 = ambient_utils.from_noise_pred_to_x0_pred_vp(x_t_prime_2, preds_prime_2, sigma_s)
                    
                    consistency_loss = (preds_prime_1 - x0_pred) * (preds_prime_2 - x0_pred)
                    consistency_loss = consistency_loss.mean()
                    loss += args.consistency_coeff * consistency_loss
                    loss = loss.mean()
                else:
                    consistency_loss = torch.tensor(0.0, device=loss.device)

                # Gather the losses across all processes for logging (if we use distributed training).
                avg_loss = accelerator.gather(loss.repeat(args.train_batch_size)).mean()
                train_loss += avg_loss.item() / args.gradient_accumulation_steps

                avg_consistency_loss = accelerator.gather(consistency_loss.repeat(args.train_batch_size)).mean()
                consistency_train_loss += avg_consistency_loss.item() / args.gradient_accumulation_steps

                # Backpropagate
                accelerator.backward(loss)
                if accelerator.sync_gradients:
                    accelerator.clip_grad_norm_(params_to_optimize, args.max_grad_norm)

                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()

                # useful for visualizing what happens before taking any gradients
                if epoch == first_epoch and args.skip_first_epoch:
                    break


            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                progress_bar.update(1)
                global_step += 1
                #accelerator.log({"train_loss": train_loss}, step=global_step)
                
                """
                # Log the two curves
                accelerator.log({
                    "timesteps_curve_b1": timesteps[0],
                    "timesteps_curve_b2": timesteps[1]
                }, step=global_step)

                accelerator.log({
                    "adjusted_timesteps_curve_b1": adjusted_timesteps[0],
                    "adjusted_timesteps_curve_b2": adjusted_timesteps[1]
                }, step=global_step)
                """
                #accelerator.log({"consistency_train_loss": consistency_train_loss}, step=global_step)
                train_loss = 0.0
                consistency_train_loss = 0.0

                if accelerator.is_main_process:
                    if global_step % args.checkpointing_steps == 0:
                        # _before_ saving state, check if this save would set us over the `checkpoints_total_limit`
                        if args.checkpoints_total_limit is not None:
                            checkpoints = os.listdir(args.output_dir)
                            checkpoints = [d for d in checkpoints if d.startswith("checkpoint")]
                            checkpoints = sorted(checkpoints, key=lambda x: int(x.split("-")[1]))

                            # before we save the new checkpoint, we need to have at _most_ `checkpoints_total_limit - 1` checkpoints
                            if len(checkpoints) >= args.checkpoints_total_limit:
                                num_to_remove = len(checkpoints) - args.checkpoints_total_limit + 1
                                removing_checkpoints = checkpoints[0:num_to_remove]

                                logger.info(
                                    f"{len(checkpoints)} checkpoints already exist, removing {len(removing_checkpoints)} checkpoints"
                                )
                                logger.info(f"removing checkpoints: {', '.join(removing_checkpoints)}")

                                for removing_checkpoint in removing_checkpoints:
                                    removing_checkpoint = os.path.join(args.output_dir, removing_checkpoint)
                                    shutil.rmtree(removing_checkpoint)

                        save_path = os.path.join(args.output_dir, f"checkpoint-{global_step}")
                        accelerator.save_state(save_path)
                        logger.info(f"Saved state to {save_path}")
                # compute FID
                #accelerator.print("global_step = {global_step}")
                if (global_step % args.FID_step_num == 0 or global_step <= 1) and args.track_fid:    
                    with torch.no_grad():
                        
                        accelerator.print("FID computation starts at step {global_step}...")
                        pipeline = StableDiffusionXLPipeline.from_pretrained(
                            args.pretrained_model_name_or_path,
                            vae=vae,
                            text_encoder=unwrap_model(text_encoder_one),
                            text_encoder_2=unwrap_model(text_encoder_two),
                            unet=unwrap_model(unet),
                            revision=args.revision,
                            variant=args.variant,
                            torch_dtype=weight_dtype,
                        )
                        pipeline = pipeline.to(accelerator.device)
                        pipeline.set_progress_bar_config(disable=True)

                        accelerator.print(f"Computing FID score with {args.num_images_for_fid} images...")
                        gen_images(pipeline, None, log_under="validation_fid", num_validation_images=args.num_images_for_fid, track_images=False)
                        accelerator.print(f"Generated {args.num_images_for_fid} images for FID computation.")
                        if accelerator.is_main_process:
                            accelerator.print("Calculating inception stats")
                            mu, sigma, inception = calculate_inception_stats(image_path=os.path.join(args.output_dir, "validation_images", str(global_step)), 
                                                                            num_expected=args.num_images_for_fid, seed=42, max_batch_size=1, 
                                                                            distributed=False,num_workers=1)
                            accelerator.print("Inception stats calculated.")
                            ref_data = np.load(args.fid_ref_path)
                            ref_mu = ref_data['mu']
                            ref_sigma = ref_data['sigma']
                            #accelerator.print("ref_mu shape:", ref_mu.shape)
                            #accelerator.print("ref_sigma shape:", ref_sigma.shape)
                            #accelerator.print("mu shape:", mu.shape)
                            #accelerator.print("sigma shape:", sigma.shape)
                            fid = calculate_fid_from_inception_stats(mu, sigma, ref_mu,ref_sigma)
                            accelerator.log({"fid": fid, "inception": inception}, step=global_step)

                        del pipeline
                        accelerator.print("FID computation finished...")

                # generate images for validation
                if (global_step % args.validation_step_num == 0 or global_step < 64) and args.track_val:
                    if accelerator.is_main_process and args.validation_prompt is not None:
                        logger.info(f"\n\n \tRunning validation at step {global_step}...")
                        # create pipeline
                    """    
                    pipeline = StableDiffusionXLPipeline.from_pretrained(
                        args.pretrained_model_name_or_path,
                        vae=vae,
                        text_encoder=unwrap_model(text_encoder_one),
                        text_encoder_2=unwrap_model(text_encoder_two),
                        unet=unwrap_model(unet),
                        revision=args.revision,
                        variant=args.variant,
                        torch_dtype=weight_dtype,
                    )
                    """
                    # Load the pipeline with explicit configuration settings
                    pipeline = StableDiffusionXLPipeline.from_pretrained(
                        args.pretrained_model_name_or_path,
                        vae=vae,
                        text_encoder=unwrap_model(text_encoder_one),
                        text_encoder_2=unwrap_model(text_encoder_two),
                        unet=unwrap_model(unet),
                        revision=args.revision,
                        variant=args.variant,
                        torch_dtype=weight_dtype,
                    )

                    pipeline = pipeline.to(accelerator.device)
                    pipeline.set_progress_bar_config(disable=True)
                    with torch.no_grad():
                        #accelerator.print("Generating images, early stopped.")
                        #gen_images(pipeline, pipe_stop_index, log_under="validation_early_stopped", distributed=False)
                        accelerator.print("Generating images, full.")
                        gen_images(pipeline, None, log_under="validation_full", distributed=False)
            

                    del pipeline
                    """
                    ambient_utils.save_images(torch.cat([model_input, noisy_model_input, x0_pred]), 
                                              os.path.join(args.output_dir, "l_nature|l_input|l_red.png"), num_rows=3, 
                                              save_wandb=True if args.report_to == "wandb" else False)
                    """
                    accelerator.print("Decoding images...")
                    with torch.no_grad():
                        dec_model_input = vae.decode(model_input.to(vae.dtype) / vae.config.scaling_factor).sample
                        dec_noisy_model_input = vae.decode(noisy_model_input.to(vae.dtype) / vae.config.scaling_factor).sample
                        dec_x0_pred = vae.decode(x0_pred.to(vae.dtype) / vae.config.scaling_factor).sample
                    del model_input, noisy_model_input, x0_pred    
                    ambient_utils.save_images(torch.cat([dec_model_input, dec_noisy_model_input, dec_x0_pred]), 
                                              os.path.join(args.output_dir, "dec_nature|dec_input|dec_pred.png"), num_rows=3,
                                              save_wandb=True if args.report_to == "wandb" else False)
                    del dec_model_input, dec_noisy_model_input, dec_x0_pred
                    accelerator.print("Finished decoding...")
                    
                    
                                
                    
            
            accelerator.log({"loss": loss}, step=global_step)
            torch.cuda.empty_cache()
            logs = {"step_loss": loss.detach().item(), "lr": lr_scheduler.get_last_lr()[0]}
            progress_bar.set_postfix(**logs)

            if global_step >= args.max_train_steps:
                break
        



    # Save the lora layers
    accelerator.wait_for_everyone()
    if accelerator.is_main_process:
        unet = unwrap_model(unet)
        unet_lora_state_dict = convert_state_dict_to_diffusers(get_peft_model_state_dict(unet))

        if args.train_text_encoder:
            text_encoder_one = unwrap_model(text_encoder_one)
            text_encoder_two = unwrap_model(text_encoder_two)

            text_encoder_lora_layers = convert_state_dict_to_diffusers(get_peft_model_state_dict(text_encoder_one))
            text_encoder_2_lora_layers = convert_state_dict_to_diffusers(get_peft_model_state_dict(text_encoder_two))
        else:
            text_encoder_lora_layers = None
            text_encoder_2_lora_layers = None

        StableDiffusionXLPipeline.save_lora_weights(
            save_directory=args.output_dir,
            unet_lora_layers=unet_lora_state_dict,
            text_encoder_lora_layers=text_encoder_lora_layers,
            text_encoder_2_lora_layers=text_encoder_2_lora_layers,
        )

        # Final inference
        if args.mixed_precision == "fp16":
            vae.to(weight_dtype)
        
        accelerator.print("Running with pre-trained pipeline...")        
        pipeline = StableDiffusionXLPipeline.from_pretrained(args.pretrained_model_name_or_path, vae=vae, 
                                                             text_encoder=accelerator.unwrap_model(text_encoder_one), 
                                                             text_encoder_2=accelerator.unwrap_model(text_encoder_two),
                                                             revision=args.revision, variant=args.variant, torch_dtype=weight_dtype)
        pipeline = pipeline.to(accelerator.device).to(weight_dtype)
        gen_images(pipeline, pipe_stop_index, log_under="test_no_lora_early_stopped")
        gen_images(pipeline, None, log_under="test_no_lora_full")


        accelerator.print("Running with pre-trained pipeline + loaded LORA weights")
        # load attention processors
        pipeline.load_lora_weights(args.output_dir, torch_dtype=torch.float32, adapter_name="default")
        pipeline = pipeline.to(accelerator.device).to(weight_dtype)
        gen_images(pipeline, pipe_stop_index, log_under="test_lora_early_stopped")
        gen_images(pipeline, None, log_under="test_lora_full")
        

    accelerator.end_training()


if __name__ == "__main__":
    # Suppress specific warnings if they are harmless
    warnings.filterwarnings("ignore", message=r".*was not found in config.*")

    args = parse_args()
    main(args)
