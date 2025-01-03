import gc
import json
import math
import pathlib
import os
import time
from argparse import ArgumentParser
from pathlib import Path
import torch
import re
import deepspeed
from deepspeed.accelerator import get_accelerator
import deepspeed.comm as dist
from huggingface_hub import snapshot_download
from transformers.utils import is_offline_mode
from transformers import (
    AutoConfig,
    TextStreamer,
)

import sys

sys.path.append(sys.path[0] + "/../../../")


import logging

logger = logging.getLogger(__name__)

from llm.inference.utils.supported_models import MODEL_CLASSES

try:
    from llava.model.builder import load_pretrained_model
    from llava.conversation import conv_templates
    from llava.mm_utils import get_model_name_from_path, tokenizer_image_token
    from llava.constants import (
        IMAGE_TOKEN_INDEX,
        DEFAULT_IMAGE_TOKEN,
        DEFAULT_IM_START_TOKEN,
        DEFAULT_IM_END_TOKEN,
    )

except ImportError:
    pass

# the Deepspeed team made these so it's super fast to load (~1 minute), rather than wait 10-20min loading time.
tp_presharded_models = [
    "microsoft/bloom-deepspeed-inference-int8",
    "microsoft/bloom-deepspeed-inference-fp16",
]

t_start = time.time()

parser = ArgumentParser()

parser.add_argument(
    "-m",
    "--model-id",
    type=str,
    default="EleutherAI/gpt-j-6b",
    help="the huggingface mdoel id",
)
parser.add_argument(
    "--vision-text-model",
    action="store_true",
    help="whether or not it is vision-text multi-model structure",
)
parser.add_argument(
    "--dtype",
    type=str,
    help="float16 or bfloat16",
    choices=["bfloat16", "float32"],
    default="bfloat16",
)
parser.add_argument(
    "--batch-size", "--batch-size", default=1, type=int, help="batch size"
)
parser.add_argument("--num-iter", default=50, type=int, help="num iter")
parser.add_argument("--num-warmup", default=5, type=int, help="num warmup")
parser.add_argument(
    "--benchmark", action="store_true", help="additionally run benchmark"
)
parser.add_argument("--greedy", action="store_true")
parser.add_argument("--profile", action="store_true")
parser.add_argument("--deployment-mode", action="store_true")
parser.add_argument("--ki", action="store_true")
parser.add_argument(
    "--max-new-tokens", default=32, type=int, help="output max new tokens"
)
parser.add_argument(
    "--streaming",
    action="store_true",
    help="enable streaming mode for generation output (greedy search only)",
)
parser.add_argument("--input-tokens", default="32", type=str)
parser.add_argument("--prompt", default=None, type=str)
parser.add_argument("--ipex", action="store_true", help="ipex is not enabled now")
parser.add_argument(
    "--ipex-weight-only-quantization",
    action="store_true",
    help="use ipex weight-only quantization",
)
parser.add_argument(
    "--local_rank", required=False, type=int, help="used by dist launchers"
)
parser.add_argument(
    "--quant-with-amp",
    action="store_true",
    help="by default it is int8-fp32 mixed, to enable int8 mixed amp bf16 (work on platforms like SPR)",
)
parser.add_argument(
    "--image-url",
    default="https://images.cocodataset.org/val2017/000000039769.jpg",
    type=str,
    help="image url for image-to-text task",
)
parser.add_argument(
    "--audio",
    default="example.flac",
    type=str,
    help="audio file for speech-to-text task",
)
parser.add_argument("--print-memory", action="store_true")
parser.add_argument("--token-latency", action="store_true")
parser.add_argument(
    "--lowp-mode",
    choices=["AUTO", "BF16", "FP32", "INT8", "FP16"],
    default="AUTO",
    type=str,
    help="low precision mode for weight only quantization. "
    "It indicates data type for computation for speedup at the cost "
    "of accuracy. Unrelated to activation or weight data type."
    "It is not supported yet to use lowp_mode=INT8 for INT8 weight, "
    "falling back to lowp_mode=BF16 implicitly in this case."
    "If set to AUTO, lowp_mode is determined by weight data type: "
    "lowp_mode=BF16 is used for INT8 weight "
    "and lowp_mode=INT8 used for INT4 weight",
)
parser.add_argument(
    "--weight-dtype",
    choices=["INT8", "INT4", "NF4"],
    default="INT8",
    type=str,
    help="weight data type for weight only quantization. Unrelated to activation data type or lowp-mode.",
)
parser.add_argument(
    "--group-size",
    default=-1,
    type=int,
    help="For weight-only quantization only. Specifies the group size along"
    " input channel for block-wise quantization of weight. It must be a"
    " positive power of 2 or -1. If it is -1, weight is quantized per"
    " output channel. Otherwise, weight is quantized per block with block size"
    " = [1, group_size]. If `--low-precision-checkpoint` is given, group"
    " size is determined automatically and this argument has no effect.",
)
parser.add_argument(
    "--act-quant-mode",
    choices=[
        "PER_TENSOR",
        "PER_IC_BLOCK",
        "PER_BATCH",
        "PER_BATCH_IC_BLOCK",
        "PER_TENSOR_SYM",
        "PER_IC_BLOCK_SYM",
        "PER_BATCH_SYM",
        "PER_BATCH_IC_BLOCK_SYM",
    ],
    default="PER_BATCH_IC_BLOCK_SYM",
    type=str,
    help="Quantization mode for activation with different granularity. "
    "For lowp-mode=INT8 only. For other cases, it has no effect. "
    "Assume the activation tensor has shape batch_size x input_channel. "
    "PER_TENSOR(0): quantize per tensor; "
    "PER_IC_BLOCK(1): quantize per group along IC with group size = IC_BLOCK; "
    "PER_BATCH(2): quantize per batch; "
    "PER_BATCH_IC_BLOCK(3): quantize per block of size 1 x IC_BLOCK. "
    "PER_TENSOR_SYM(4): symmetrically quantize per tensor; "
    "PER_IC_BLOCK_SYM(5): symmetrically quantize per group along IC with group size = IC_BLOCK; "
    "PER_BATCH_SYM(6): symmetrically quantize per batch; "
    "PER_BATCH_IC_BLOCK_SYM(7): symmetrically quantize per block of size 1 x IC_BLOCK. "
    "IC_BLOCK is determined by IC automatically.",
)
parser.add_argument(
    "--cache-weight-for-large-batch",
    action="store_true",
    help="Cache an extra linear weight for large batch inference, such as the first token (prefill phase)."
    " It brings better performance at the cost of higher memory usage. It is only valid for full bf16 path"
    " and weight-only quantization with lowp-mode=BF16. Otherwise, it has no effect.",
)
parser.add_argument(
    "--config-file", default=None, type=str, help="specific configuration file"
)
parser.add_argument(
    "--woq-sym-quant-weight",
    action="store_true",
    help="Quantize weight symmetrically for weight only quantization. It usually brings better latency at"
    " the cost of accuracy. It has not effect if you are loading low-precision checkpoints.",
)
parser.add_argument(
    "--kv-cache-dtype",
    type=str,
    choices=[
        "auto",
        "fp8_e5m2",
    ],
    default="auto",
    help='Data type for kv cache storage. If "auto", will use model '
    "data type. fp8 type now supports e5m2.",
)
parser.add_argument(
    "--low-precision-checkpoint",
    default="",
    type=str,
    help="Low precision checkpoint file generated by algorithms, such as GPTQ. It contains"
    " INT4 weights, scales, zero points, etc. For better accuracy of weight only"
    " quantization with INT4 weight.",
)


args = parser.parse_args()


num_tokens = args.max_new_tokens
use_ipex = args.ipex or args.ipex_weight_only_quantization

# import extension
if use_ipex:
    import intel_extension_for_pytorch as ipex

    torch._C._jit_set_texpr_fuser_enabled(False)
    try:
        ipex._C.disable_jit_linear_repack()
    except Exception:
        pass


def get_int_from_env(env_keys, default):
    """Returns the first positive env value found in the `env_keys` list or the default."""
    for e in env_keys:
        val = int(os.environ.get(e, -1))
        if val >= 0:
            return val
    return default


local_rank = get_int_from_env(["LOCAL_RANK", "MPI_LOCALRANKID"], "0")
world_size = get_int_from_env(["WORLD_SIZE", "PMI_SIZE"], "1")

deepspeed.init_distributed(get_accelerator().communication_backend_name())


def print_rank0(*msg):
    if local_rank != 0:
        return
    print(*msg)


# Model loading and instantiating on GPUs
def get_repo_root(model_name_or_path):
    if os.path.exists(model_name_or_path):
        # local path
        return model_name_or_path
    # checks if online or not
    if is_offline_mode():
        print_rank0("Offline mode: forcing local_files_only=True")
    # download only on first process
    allow_patterns = ["*.bin", "*.model", "*.json", "*.txt", "*.py", "*LICENSE"]
    if local_rank == 0:
        snapshot_download(
            model_name_or_path,
            local_files_only=is_offline_mode(),
            cache_dir=os.getenv("TRANSFORMERS_CACHE", None),
            allow_patterns=allow_patterns,
            # ignore_patterns=["*.safetensors"],
        )

    dist.barrier()

    return snapshot_download(
        model_name_or_path,
        local_files_only=is_offline_mode(),
        cache_dir=os.getenv("TRANSFORMERS_CACHE", None),
        allow_patterns=allow_patterns,
        # ignore_patterns=["*.safetensors"],
    )


def get_checkpoint_files(model_name_or_path):
    cached_repo_dir = get_repo_root(model_name_or_path)

    # extensions: .bin | .pt
    # creates a list of paths from all downloaded files in cache dir
    file_list = [
        str(entry)
        for entry in Path(cached_repo_dir).rglob("*.[bp][it][n]")
        if entry.is_file()
    ]
    return file_list


model_name = args.model_id
if args.quant_with_amp:
    load_dtype = torch.bfloat16
    infer_dtype = torch.bfloat16
else:
    if args.dtype == "bfloat16":
        load_dtype = torch.bfloat16
        infer_dtype = torch.bfloat16
    else:
        load_dtype = torch.float32
        infer_dtype = torch.float32

tp_presharded_mode = True if model_name in tp_presharded_models else False

# print(get_checkpoint_files(model_name))

print_rank0(f"*** Loading the model {model_name}")
model_type = next((x for x in MODEL_CLASSES.keys() if x in model_name.lower()), "auto")
if model_type == "llama" and args.vision_text_model:
    model_type = "mllama"
if model_type == "maira-2":
    model_type = "maira2"
model_class = MODEL_CLASSES[model_type]
tokenizer = model_class[1].from_pretrained(model_name, trust_remote_code=True)

if model_type == "auto":
    if args.config_file is None:
        config = AutoConfig.from_pretrained(
            args.model_id, torchscript=True, trust_remote_code=True
        )
    else:
        config = AutoConfig.from_pretrained(
            args.config_file, torchscript=True, trust_remote_code=True
        )
    if re.search("falcon", config.architectures[0], re.IGNORECASE) or re.search(
        "rw", config.architectures[0], re.IGNORECASE
    ):
        model_type = "falcon"
    if re.search("gptbigcode", config.architectures[0], re.IGNORECASE):
        model_type = "gptbigcode"
    if re.search("gptneox", config.architectures[0], re.IGNORECASE):
        model_type = "gpt-neox"

if model_type == "falcon":
    model_input_names = ["input_ids", "attention_mask"]
    tokenizer.model_input_names = model_input_names

if args.config_file is None:
    if model_type == "chatglm":
        config = AutoConfig.from_pretrained(
            args.model_id,
            torchscript=True,
            trust_remote_code=True,
            torch_dtype=load_dtype,
        )
    else:
        config = AutoConfig.from_pretrained(
            args.model_id, torchscript=True, trust_remote_code=True
        )
else:
    config = AutoConfig.from_pretrained(
        args.config_file, torchscript=True, trust_remote_code=True
    )

if args.kv_cache_dtype == "auto":
    kv_cache_dtype = None
elif args.kv_cache_dtype == "fp8_e5m2":
    kv_cache_dtype = torch.float8_e5m2
config.kv_cache_dtype = kv_cache_dtype

if not hasattr(config, "text_max_length") and args.prompt is None:
    config.text_max_length = int(args.input_tokens) + int(args.max_new_tokens)
if model_type == "mpt" and args.prompt is None:
    config.max_seq_len = int(args.input_tokens) + int(args.max_new_tokens)
if model_type == "whisper":
    config.text_max_length = config.max_source_positions + config.max_target_positions
if model_type == "llava":
    config.use_cache = True

if not hasattr(config, "lm_head_generation"):
    config.lm_head_generation = True
if model_type == "maira2" and not hasattr(config.text_config, "lm_head_generation"):
    config.text_config.lm_head_generation = True
num_beams = 1 if args.greedy else 4
if model_type in ["git", "llava"]:
    config.batch_size = int(args.batch_size) * num_beams
# XXX: can't automatically derive dtype via config's `from_pretrained`
# dtype = torch.bfloat16 if model_name in ["bigscience/bloom", "bigscience/bigscience-small-testing"] else torch.float16


# use one of these args to `init_inference`
# 1. injection_policy is the slower version, but it's plain pytorch so it'll always work
# 2. replace_with_kernel_inject is the faster one (fast fused kernels)
kernel_inject = args.ki

if args.benchmark:
    get_accelerator().empty_cache()
    gc.collect()
    deepspeed.runtime.utils.see_memory_usage("pre-from-pretrained", force=True)

# For now, Falcon, baichuan, baichuan2, and gptbigcode have accuracy issue with from_config with deepspeed meta device load.
# TODO: we will change the scope once deepspeed providing the support

if model_type in ["llava"]:
    tokenizer, model, image_processor, context_len = load_pretrained_model(
        args.model_id
    )
    model.config = config
elif world_size == 1 or model_type in [
    "falcon",
    "baichuan",
    "baichuan2",
    "gptbigcode",
    "git",
    "mllama",
    "qwen",
    "yuan",
    "whisper",
]:
    model = model_class[0].from_pretrained(
        model_name,
        config=config,
        low_cpu_mem_usage=True if model_type != "maira2" else False,
        torch_dtype=load_dtype,
        trust_remote_code=True,
    )
elif model_type == "maira2":
    model = model_class[0].from_pretrained(
        model_name,
        torch_dtype=load_dtype,
        trust_remote_code=True,
    )
else:  # Construct model with fake meta tensors, later will be replaced during ds-inference ckpt load
    with deepspeed.OnDevice(dtype=load_dtype, device="meta"):
        if model_type in ["t5"]:
            model = model_class[0](config=config)
        else:
            model = (
                model_class[0]
                .from_config(config, trust_remote_code=True)
                .to(load_dtype)
            )

if args.benchmark:
    deepspeed.runtime.utils.see_memory_usage("post-from-pretrained", force=True)

model = model.eval()
model = model.to(memory_format=torch.channels_last)

if args.benchmark:
    get_accelerator().empty_cache()
    gc.collect()
    deepspeed.runtime.utils.see_memory_usage("post-init-ds-zero-init", force=True)

# Deepspeed-Inference Loading

checkpoints_json = "checkpoints.json"


def write_checkpoints_json():
    checkpoint_files = get_checkpoint_files(model_name)
    if local_rank == 0:
        # model.config.model_type.upper()
        data = {"type": "BLOOM", "checkpoints": checkpoint_files, "version": 1.0}
        json.dump(data, open(checkpoints_json, "w"))


if args.benchmark:
    get_accelerator().empty_cache()
    gc.collect()
    deepspeed.runtime.utils.see_memory_usage("pre-ds-inference-init", force=True)

if kernel_inject:
    kwargs = dict(replace_with_kernel_inject=True)
else:
    kwargs = dict(replace_with_kernel_inject=False)

repo_root = get_repo_root(model_name)
if tp_presharded_mode:
    # tp presharded repos come with their own checkpoints config file
    checkpoints_json = os.path.join(repo_root, "ds_inference_config.json")
else:
    # for normal bloom repo we need to write the checkpoints config file
    write_checkpoints_json()
    dist.barrier()

tp_grain_size = 64
# Need to check if this attr is available. Old DeepSpeep does not have it.
if "tp_grain_size" in dir(deepspeed.inference.config.DeepSpeedTPConfig()):
    if args.ipex_weight_only_quantization and args.low_precision_checkpoint != "":
        pathname = args.low_precision_checkpoint
        assert os.path.exists(pathname), f"Checkpoint file does not exist: {pathname}"
        if os.path.isdir(pathname):
            try:
                with open(pathname + "/config.json") as f:
                    quant_model_config = json.load(f)
                    tp_grain_size = int(
                        quant_model_config["quantization_config"]["group_size"]
                    )
            except Exception as e:
                print("Failed to get group_size from config.json")
        elif args.group_size > 0:
            tp_grain_size = args.group_size
        else:
            print(
                "Warning: cannot get group_size from config.json or --group-size, "
                "using default value 64 for tp_grain_size"
            )
    kwargs.update(
        {
            "tensor_parallel": deepspeed.inference.config.DeepSpeedTPConfig(
                tp_grain_size=tp_grain_size
            )
        }
    )

model = deepspeed.init_inference(
    model,
    mp_size=world_size,
    base_dir=repo_root,
    dtype=infer_dtype,
    checkpoint=checkpoints_json,
    **kwargs,
)

if args.benchmark:
    get_accelerator().empty_cache()
    gc.collect()
    deepspeed.runtime.utils.see_memory_usage("post-ds-inference-init", force=True)


model = model.module

if args.benchmark:
    t_ready = time.time()

# to ipex
if use_ipex:
    ipex_woq_enabled = args.ipex_weight_only_quantization
    low_precision_checkpoint = None
    if ipex_woq_enabled:
        if args.low_precision_checkpoint != "":
            pathname = args.low_precision_checkpoint
            assert os.path.exists(
                pathname
            ), f"Checkpoint file does not exist: {pathname}"
            if os.path.isfile(pathname):
                if pathname.endswith(".pt") or pathname.endswith(".pth"):
                    low_precision_checkpoint = torch.load(pathname, weights_only=True)
                elif pathname.endswith(".safetensors"):
                    try:
                        import safetensors
                    except ImportError:
                        print(
                            "Please install safetensors package to load safetensors checkpoint."
                        )
                        exit(1)
                    low_precision_checkpoint = safetensors.torch.load_file(pathname)
                assert (
                    low_precision_checkpoint is not None
                ), f"Invalid checkpoint file: {pathname}. Should be a .pt, .pth or .safetensors file."

                quant_method = {"quant_method": "gptq"}

            elif os.path.isdir(pathname):
                low_precision_checkpoint = {}
                for pattern in ["*.pt", "*.pth"]:
                    files = list(pathlib.Path(pathname).glob(pattern))
                    if files:
                        for f in files:
                            data_f = torch.load(f, weights_only=True)
                            low_precision_checkpoint.update(data_f)
                        break
                if not low_precision_checkpoint:
                    files = list(pathlib.Path(pathname).glob("*.safetensors"))
                    if files:
                        try:
                            import safetensors
                        except ImportError:
                            print(
                                "Please install safetensors package to load safetensors checkpoint."
                            )
                            exit(1)
                        for f in files:
                            data_f = safetensors.torch.load_file(f)
                            low_precision_checkpoint.update(data_f)
                assert (
                    len(low_precision_checkpoint) > 0
                ), f"Cannot find checkpoint (.pt/.pth/.safetensors) files in path {pathname}."

                try:
                    with open(pathname + "/config.json") as f:
                        quant_model_config = json.load(f)
                    quant_method = {
                        "quant_method": quant_model_config["quantization_config"][
                            "quant_method"
                        ]
                    }
                except Exception as e:
                    print(
                        "warning: loading HF config.json to get `quant_method` failed, due to ",
                        e,
                    )
                    print("warning: specifying `quant_method` = `gptq` by default.")
                    quant_method = {"quant_method": "gptq"}

            else:
                raise AssertionError(
                    f"Invalid low-precision-checkpoint: {pathname}."
                    " Should be a .pt/.pth/.safetensors file or a directory containing them."
                )

            low_precision_checkpoint = (low_precision_checkpoint, quant_method)
            low_precision_checkpoint_dict = low_precision_checkpoint[0]
        else:
            low_precision_checkpoint = None
        from intel_extension_for_pytorch.quantization import (
            WoqWeightDtype,
            WoqWeightQScheme,
        )

        if args.weight_dtype == "INT8":
            weight_dtype = WoqWeightDtype.INT8
        elif args.weight_dtype == "INT4":
            weight_dtype = WoqWeightDtype.INT4
        else:
            assert args.weight_dtype == "NF4"
            weight_dtype = WoqWeightDtype.NF4
        if args.lowp_mode == "INT8":
            lowp_mode = ipex.quantization.WoqLowpMode.INT8
        elif args.lowp_mode == "FP32":
            lowp_mode = ipex.quantization.WoqLowpMode.NONE
        elif args.lowp_mode == "FP16":
            lowp_mode = ipex.quantization.WoqLowpMode.FP16
        elif args.lowp_mode == "BF16":
            lowp_mode = ipex.quantization.WoqLowpMode.BF16
        else:  # AUTO
            if weight_dtype == WoqWeightDtype.INT4:
                lowp_mode = ipex.quantization.WoqLowpMode.INT8
            else:
                lowp_mode = ipex.quantization.WoqLowpMode.BF16

        act_quant_mode_dict = {
            "PER_TENSOR": ipex.quantization.WoqActQuantMode.PER_TENSOR,
            "PER_IC_BLOCK": ipex.quantization.WoqActQuantMode.PER_IC_BLOCK,
            "PER_BATCH": ipex.quantization.WoqActQuantMode.PER_BATCH,
            "PER_BATCH_IC_BLOCK": ipex.quantization.WoqActQuantMode.PER_BATCH_IC_BLOCK,
            "PER_TENSOR_SYM": ipex.quantization.WoqActQuantMode.PER_TENSOR_SYM,
            "PER_IC_BLOCK_SYM": ipex.quantization.WoqActQuantMode.PER_IC_BLOCK_SYM,
            "PER_BATCH_SYM": ipex.quantization.WoqActQuantMode.PER_BATCH_SYM,
            "PER_BATCH_IC_BLOCK_SYM": ipex.quantization.WoqActQuantMode.PER_BATCH_IC_BLOCK_SYM,
        }
        weight_qscheme = (
            WoqWeightQScheme.SYMMETRIC
            if args.woq_sym_quant_weight
            else WoqWeightQScheme.UNDEFINED
        )
        qconfig = ipex.quantization.get_weight_only_quant_qconfig_mapping(
            weight_dtype=weight_dtype,
            lowp_mode=lowp_mode,
            act_quant_mode=act_quant_mode_dict[args.act_quant_mode],
            group_size=args.group_size,
            weight_qscheme=weight_qscheme,
        )
        if low_precision_checkpoint is not None:
            num_heads = model.config.num_attention_heads
            if hasattr(model.config, "num_key_value_heads"):
                num_heads = model.config.num_key_value_heads
            rank = local_rank

            mha_layers_split_by_N = [
                "q_proj",
                "k_proj",
                "v_proj",
            ]
            # mlp is split with grain size = tp_grain_size
            mlp_layers_split_by_N = [
                "gate_proj",
                "up_proj",
                "fc_in",
                "fc1",
                "query_key_value",
                "w1",
                "w3",
            ]
            mha_layers_split_by_K = [
                "o_proj",
                "out_proj",
            ]
            # mlp is split with grain size = tp_grain_size
            mlp_layers_split_by_K = [
                "down_proj",
                "fc_out",
                "fc2",
                "dense",
                "dense_4h_to_h",
                "w2",
            ]
            # lm_head is split with grain size = tp_grain_size
            lm_head_layers = ["lm_head"]  # split by K but not quantized
            quantization_method = quant_model_config["quantization_config"][
                "quant_method"
            ]
            head_range = [0]
            head_per_rank = num_heads // world_size
            for i in range(0, world_size):
                head_this_rank = head_per_rank
                if i < num_heads % world_size:
                    head_this_rank += 1
                head_range.append(head_range[-1] + head_this_rank)
            for key in low_precision_checkpoint[0].keys():
                q_head_start = head_range[rank]
                q_head_end = q_head_start + (head_range[rank + 1] - head_range[rank])
                if "bias" in key:
                    continue
                if any(substring in key for substring in mha_layers_split_by_N):
                    data = low_precision_checkpoint_dict[key]
                    if quantization_method == "awq":
                        # awq qweight: [K, N // 8]
                        # awq scales: [K // G, N]
                        # awq qzeros: [K // G, N // 8]
                        if data.shape[-1] % head_range[-1] == 0:
                            dim = data.shape[-1] // head_range[-1]
                        else:
                            assert data.shape[-1] % world_size == 0
                            dim = data.shape[-1] // world_size
                            q_head_start = local_rank
                            q_head_end = local_rank + 1
                        low_precision_checkpoint_dict[key] = data[
                            :, q_head_start * dim : q_head_end * dim
                        ]
                    else:
                        raise AssertionError(
                            f"{quantization_method} is not supported yet."
                        )
                elif any(substring in key for substring in mlp_layers_split_by_N):
                    data = low_precision_checkpoint_dict[key]
                    if quantization_method == "awq":
                        # awq qweight: [K, N // 8]
                        # awq scales: [K // G, N]
                        # awq qzeros: [K // G, N // 8]
                        if "scales" in key:
                            assert (
                                data.shape[1] % tp_grain_size == 0
                            ), "N must be divisible by tp_grain_size"
                            grains = data.shape[1] // tp_grain_size
                            dim = tp_grain_size
                        else:
                            assert (
                                data.shape[1] * 8
                            ) % tp_grain_size == 0, (
                                "N must be divisible by tp_grain_size"
                            )
                            grains = data.shape[1] // (tp_grain_size // 8)
                            dim = tp_grain_size // 8
                        grains_per_rank = grains // world_size
                        grains_rem = grains % world_size
                        grains_start = grains_per_rank * local_rank + min(
                            local_rank, grains_rem
                        )
                        grains_end = (
                            grains_start
                            + grains_per_rank
                            + (1 if local_rank < grains_rem else 0)
                        )
                        low_precision_checkpoint_dict[key] = data[
                            :, grains_start * dim : grains_end * dim
                        ]
                    else:
                        raise AssertionError(
                            f"{quantization_method} is not supported yet."
                        )
                elif any(substring in key for substring in mha_layers_split_by_K):
                    data = low_precision_checkpoint_dict[key]
                    if quantization_method == "awq":
                        # awq qweight: [K, N // 8]
                        # awq scales: [K // G, N]
                        # awq qzeros: [K // G, N // 8]
                        if data.shape[0] % head_range[-1] == 0:
                            dim = data.shape[0] // head_range[-1]
                        else:
                            assert data.shape[0] % world_size == 0
                            dim = data.shape[0] // world_size
                            q_head_start = local_rank
                            q_head_end = local_rank + 1
                        low_precision_checkpoint_dict[key] = data[
                            q_head_start * dim : q_head_end * dim
                        ]
                    else:
                        raise AssertionError(
                            f"{quantization_method} is not supported yet."
                        )
                elif any(substring in key for substring in mlp_layers_split_by_K):
                    data = low_precision_checkpoint_dict[key]
                    if quantization_method == "awq":
                        # awq qweight: [K, N // 8]
                        # awq scales: [K // G, N]
                        # awq qzeros: [K // G, N // 8]
                        if "qweight" in key:
                            assert (
                                data.shape[0] % tp_grain_size == 0
                            ), "K must be divisible by tp_grain_size"
                            grains = data.shape[0] // tp_grain_size
                            dim = tp_grain_size
                        else:
                            grains = data.shape[0]
                            dim = 1
                        grains_per_rank = grains // world_size
                        grains_rem = grains % world_size
                        grains_start = grains_per_rank * local_rank + min(
                            local_rank, grains_rem
                        )
                        grains_end = (
                            grains_start
                            + grains_per_rank
                            + (1 if local_rank < grains_rem else 0)
                        )
                        low_precision_checkpoint_dict[key] = data[
                            grains_start * dim : grains_end * dim
                        ]
                    else:
                        raise AssertionError(
                            f"{quantization_method} is not supported yet."
                        )
                elif any(substring in key for substring in lm_head_layers):
                    # lm_head: [N, K] (not quantized)
                    # Same for both AWQ and GPTQ
                    data = low_precision_checkpoint_dict[key]
                    assert (
                        data.shape[1] % tp_grain_size == 0
                    ), "K must be divisible by tp_grain_size"
                    grains = data.shape[1] // tp_grain_size
                    dim = tp_grain_size
                    grains_per_rank = grains // world_size
                    grains_rem = grains % world_size
                    grains_start = grains_per_rank * local_rank + min(
                        local_rank, grains_rem
                    )
                    grains_end = (
                        grains_start
                        + grains_per_rank
                        + (1 if local_rank < grains_rem else 0)
                    )
                    low_precision_checkpoint_dict[key] = data[
                        :, grains_start * dim : grains_end * dim
                    ]
            low_precision_checkpoint = (low_precision_checkpoint_dict, quant_method)

        else:
            low_precision_checkpoint = None

    model = ipex.llm.optimize(
        model.eval(),
        dtype=infer_dtype,
        quantization_config=qconfig if ipex_woq_enabled else None,
        inplace=True,
        deployment_mode=args.deployment_mode,
        cache_weight_for_large_batch=args.cache_weight_for_large_batch,
        low_precision_checkpoint=low_precision_checkpoint,
    )
# Generate
print_rank0(f"*** Starting to generate {num_tokens} tokens with bs={args.batch_size}")

if args.streaming:
    streamer = TextStreamer(tokenizer)
else:
    streamer = None
generate_kwargs = dict(
    do_sample=False,
    num_beams=num_beams,
    max_new_tokens=args.max_new_tokens,
    min_new_tokens=args.max_new_tokens,
    streamer=streamer,
)


if args.token_latency and not use_ipex:
    args.token_latency = False
    logger.warning(
        "--token-latency requires using ipex (--ipex or --ipex-weight-only-quantization). Disabling --token-latency."
    )
if args.token_latency:
    if not hasattr(model.config, "token_latency"):
        model.config.token_latency = True

if re.search("t5", model.config.architectures[0], re.IGNORECASE):
    generate_kwargs["max_length"] = generate_kwargs["max_new_tokens"]
    generate_kwargs.pop("max_new_tokens")
print_rank0(f"Generate args {generate_kwargs}")

if model_type == "git":
    from PIL import Image
    import requests

    prompt = Image.open(requests.get(args.image_url, stream=True).raw)
    inputs = [prompt] * args.batch_size
    generate_kwargs.pop("min_new_tokens", None)
elif model_type == "llava":
    from PIL import Image
    import requests
    from io import BytesIO

    def load_image(image_file):
        if image_file.startswith("http://") or image_file.startswith("https://"):
            response = requests.get(image_file)
            image = Image.open(BytesIO(response.content)).convert("RGB")
        else:
            image = Image.open(image_file).convert("RGB")
        return image

    model_name = get_model_name_from_path(args.model_id)
    if "llama-2" in model_name.lower():
        conv_mode = "llava_llama_2"
    elif "v1" in model_name.lower():
        conv_mode = "llava_v1"
    elif "mpt" in model_name.lower():
        conv_mode = "mpt"
    else:
        conv_mode = "llava_v0"
    conv = conv_templates[conv_mode].copy()
    if "mpt" in model_name.lower():
        roles = ("user", "assistant")
    else:
        roles = conv.roles
    if args.prompt is not None:
        prompt = args.prompt
    image = load_image(args.image_url)
    image = [image] * args.batch_size
    if model.config.mm_use_im_start_end:
        prompt = (
            DEFAULT_IM_START_TOKEN
            + DEFAULT_IMAGE_TOKEN
            + DEFAULT_IM_END_TOKEN
            + "\n"
            + prompt
        )
    else:
        prompt = DEFAULT_IMAGE_TOKEN + "\n" + prompt
    conv.append_message(conv.roles[0], prompt)
    conv.append_message(conv.roles[1], None)
    prompt = conv.get_prompt()
    inputs = [prompt] * args.batch_size
elif model_type == "whisper":
    import librosa

    sample = librosa.load(args.audio, sr=16000)
    prompt = sample[0]
    inputs = [prompt] * args.batch_size
    generate_kwargs.pop("min_new_tokens", None)
elif model_type == "mllama":
    from PIL import Image

    def load_image(image_file):
        if image_file.startswith("http://") or image_file.startswith("https://"):
            import requests

            raw_image = Image.open(requests.get(args.image_url, stream=True).raw)
        else:
            raw_image = Image.open(image_file)
        return raw_image

    current_path = pathlib.Path(__file__).parent.resolve()
    with open(str(current_path) + "/prompt.json") as f:
        prompt_pool = json.load(f)
    if args.prompt is not None:
        prompt = args.prompt
    elif model_type == "auto":
        raise SystemExit(
            "[ERROR] model prompt is not supported, please use --prompt for this model: "
            + args.model_id
        )
    # elif int(args.input_tokens) > 8192:
    #     prompt = prompt_pool[model_type]["8192"] * int(int(args.input_tokens) / 8192)
    elif args.input_tokens in prompt_pool[model_type]:
        prompt = prompt_pool[model_type][args.input_tokens]
    else:
        raise SystemExit("[ERROR] Plese use --prompt if want to use custom input.")

    raw_image = load_image(args.image_url)
    raw_image = [raw_image] * args.batch_size
    inputs = tokenizer(raw_image, prompt, return_tensors="pt")
    input_size = inputs["input_ids"].size(dim=1)
    print("---- Prompt size:", input_size)
    inputs = [prompt] * args.batch_size
elif model_type == "maira2":
    from PIL import Image
    import requests

    def download_and_open(url: str) -> Image.Image:
        response = requests.get(url, headers={"User-Agent": "MAIRA-2"}, stream=True)
        return Image.open(response.raw)

    prompt = args.prompt
    sample = download_and_open(args.image_url)
    process_input_func = (
        tokenizer.process_reporting_input
        if hasattr(tokenizer, "process_reporting_input")
        else tokenizer.format_and_preprocess_reporting_input
    )
    inputs = [prompt] * args.batch_size
else:
    # input tokens
    input_sentences = []
    current_path = pathlib.Path(__file__).parent.resolve()
    with open(str(current_path) + "/prompt.json") as f:
        prompt_pool = json.load(f)
    if model_type == "gptj":
        model_type = "gpt-j"
    if model_type == "gptneox":
        model_type = "gpt-neox"
    if args.prompt is not None:
        input_sentences.append(args.prompt)
    elif model_type == "auto":
        raise SystemExit(
            "[ERROR] model prompt is not supported, please use --prompt for this model: "
            + args.model_id
        )
    # elif int(args.input_tokens) > 8192:
    #     input_sentences.append(
    #         prompt_pool[model_type]["8192"] * int(int(args.input_tokens) / 8192)
    #     )
    elif args.input_tokens in prompt_pool[model_type]:
        input_sentences.append(prompt_pool[model_type][args.input_tokens])
    else:
        raise SystemExit("[ERROR] Plese use --prompt if want to use custom input.")
    if args.batch_size > len(input_sentences):
        # dynamically extend to support larger bs by repetition
        input_sentences *= math.ceil(args.batch_size / len(input_sentences))

    inputs = input_sentences[: args.batch_size]
    input_size = tokenizer.batch_encode_plus(
        inputs, return_tensors="pt"
    ).input_ids.size(dim=1)
    print("*** Prompt size: ", input_size)


def generate():
    """returns a list of zipped inputs, outputs and number of new tokens"""

    if model_type == "git":
        input_tokens = tokenizer(images=inputs, return_tensors="pt")
        input_ids = input_tokens.pixel_values
    elif model_type == "llava":
        input_ids = torch.stack(
            [
                tokenizer_image_token(
                    pmt, tokenizer, IMAGE_TOKEN_INDEX, return_tensors="pt"
                )
                for pmt in inputs
            ]
        )
        image_tensor = [
            image_processor.preprocess(img, return_tensors="pt")["pixel_values"].to(
                infer_dtype
            )
            for img in image
        ]
        input_tokens = {"input_ids": input_ids, "images": image_tensor}
    elif model_type == "whisper":
        input_tokens = tokenizer(inputs, sampling_rate=16000, return_tensors="pt")
        input_ids = input_tokens.input_features
    elif model_type == "mllama":
        raw_image = load_image(args.image_url)
        raw_image = [raw_image] * args.batch_size
        input_tokens = tokenizer(raw_image, prompt, return_tensors="pt")
        input_ids = input_tokens["input_ids"]
    elif model_type == "maira2":
        input_tokens = process_input_func(
            current_frontal=sample,
            current_lateral=None,
            prior_frontal=None,
            indication=None,
            technique=None,
            comparison=None,
            prior_report=None,
            return_tensors="pt",
            get_grounding=False,
        )
        input_ids = input_tokens["input_ids"]
    else:
        input_tokens = tokenizer.batch_encode_plus(
            inputs, return_token_type_ids=False, return_tensors="pt"
        )
        input_ids = input_tokens.input_ids
    for t in input_tokens:
        if torch.is_tensor(input_tokens[t]):
            input_tokens[t] = input_tokens[t].to(
                get_accelerator().current_device_name()
            )
    with torch.inference_mode(), torch.no_grad(), torch.cpu.amp.autocast(
        enabled=True if infer_dtype == torch.bfloat16 else False
    ):
        outputs = model.generate(**input_tokens, **generate_kwargs)
    gen_ids = outputs[0] if args.token_latency else outputs

    input_tokens_lengths = [x.shape[0] for x in input_ids]
    output_tokens_lengths = [x.shape[0] for x in gen_ids]

    total_new_tokens = [
        o if model.config.model_type in ["t5", "whisper"] else o - i
        for i, o in zip(input_tokens_lengths, output_tokens_lengths)
    ]
    gen_text = tokenizer.batch_decode(
        (
            gen_ids[:, input_ids.shape[1] :]
            if model_type in ["llava", "maira2"]
            else gen_ids
        ),
        skip_special_tokens=True,
    )

    return zip(inputs, gen_text, total_new_tokens), outputs


def trace_handler(prof):
    print(prof.key_averages().table(sort_by="self_cpu_time_total", row_limit=-1))


# warmup is a must if measuring speed as it's when all the optimizations are performed
# e.g. on 8x80 a100 the first pass of 100 tokens takes 23sec, and the next one is 4secs
if not args.benchmark:
    print_rank0("*** Running generate warmup")
    generated, _ = generate()

    print_rank0("*** Running generate")
    t_generate_start = time.time()
    generated, _ = generate()
    t_generate_span = time.time() - t_generate_start
    for i, o, _ in generated:
        print_rank0(f"{'-'*60}\nin={i}\nout={o}\n")

# benchmark it!
else:
    get_accelerator().empty_cache()
    gc.collect()
    deepspeed.runtime.utils.see_memory_usage("end-of-run", force=True)

    print_rank0("*** Running benchmark")
    total_time = 0.0
    cycles = args.num_iter
    warmup = args.num_warmup
    total_list = []
    # latency
    for i in range(cycles):
        t0 = time.time()
        gen_ids, outputs = generate()
        t1 = time.time()
        gen_ids = list(gen_ids)
        print_rank0(gen_ids[0][1:])
        print_rank0("Iteration: %d, Time: %.6f sec" % (i, t1 - t0))
        if i >= warmup:
            total_time += t1 - t0
            if args.token_latency:
                total_list.append(outputs[1])

    if args.profile:
        # Wait for all ranks to finish before move on
        deepspeed.comm.barrier()
        with torch.profiler.profile(
            activities=[torch.profiler.ProfilerActivity.CPU],
            schedule=torch.profiler.schedule(wait=1, warmup=3, active=1),
            on_trace_ready=trace_handler,
        ) as prof:
            for i in range(5):
                gen_ids, outputs = generate()
                prof.step()
        # Wait for all ranks to finish before move on
        deepspeed.comm.barrier()

    latency = total_time / (cycles - warmup) * 1000
    print_rank0("\n", "-" * 10, "Summary:", "-" * 10)
    print_rank0("Inference latency: %.2f ms." % latency)
    if args.token_latency:
        import numpy as np
        from itertools import chain

        first_latency = np.mean([x[0] for x in total_list]) * 1000
        average_2n = list(chain(*[x[1:] for x in total_list]))
        average_2n.sort()
        average_2n_latency = np.mean(average_2n) * 1000
        p90_latency = average_2n[int(len(average_2n) * 0.9)] * 1000
        p99_latency = average_2n[int(len(average_2n) * 0.99)] * 1000
        print_rank0("First token average latency: %.2f ms." % first_latency)
        print_rank0("Average 2... latency: %.2f ms." % average_2n_latency)
        print_rank0("P90 2... latency: %.2f ms." % p90_latency)
        print_rank0("P99 2... latency: %.2f ms." % p99_latency)
