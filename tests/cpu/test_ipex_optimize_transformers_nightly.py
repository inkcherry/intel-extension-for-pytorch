import unittest
import torch
import intel_extension_for_pytorch as ipex
import intel_extension_for_pytorch._C as core
import sys
import subprocess
import os
import copy
import re
from collections import namedtuple
import itertools

from hf_configs.baichuan.modeling_baichuan import BaichuanForCausalLM
from hf_configs.chatglm.modeling_chatglm import ChatGLMForConditionalGeneration
from hf_configs.qwen.modeling_qwen import QWenLMHeadModel
from hf_configs.llava.modeling_llavallama import LlavaLlamaForCausalLM
from hf_configs.yuan.yuan_hf_model import YuanForCausalLM
from hf_configs.phi.modeling_phi import PhiForCausalLM
from hf_configs.phi3.modeling_phi3 import Phi3ForCausalLM
from hf_configs.maira2.modeling_maira2 import Maira2ForConditionalGeneration
from intel_extension_for_pytorch.cpu._auto_kernel_selection import _disable_tpp

try:
    import transformers
    from transformers import AutoConfig
except ImportError:
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "transformers==4.46.2"]
    )
    import transformers
    from transformers import AutoConfig

from common_utils import TestCase

torch.manual_seed(128)

curpath = os.path.abspath(os.path.dirname(__file__))

model_info = namedtuple(
    "model_info",
    "name, model_class, has_position_ids, attention_class, decoder_class",
)
supported_models = [
    model_info(
        "gptneox",
        transformers.models.gpt_neox.modeling_gpt_neox.GPTNeoXForCausalLM,
        True,
        lambda m: m.gpt_neox.layers[0].attention.__class__,
        None,
    ),
    model_info(
        "opt",
        transformers.models.opt.modeling_opt.OPTForCausalLM,
        False,
        lambda m: m.model.decoder.layers[0].self_attn.__class__,
        lambda m: m.model.decoder.layers[0].__class__,
    ),
    model_info(
        "falcon",
        transformers.models.falcon.modeling_falcon.FalconForCausalLM,
        False,
        lambda m: m.transformer.h[0].self_attention.__class__,
        lambda m: m.transformer.h[0].__class__,
    ),
    model_info(
        "bloom",
        transformers.models.bloom.modeling_bloom.BloomForCausalLM,
        False,
        lambda m: m.transformer.h[0].self_attention.__class__,
        lambda m: m.transformer.h[0].__class__,
    ),
    model_info(
        "codegen",
        transformers.models.codegen.modeling_codegen.CodeGenForCausalLM,
        True,
        lambda m: m.transformer.h[0].attn.__class__,
        lambda m: m.transformer.h[0].__class__,
    ),
    model_info(
        "baichuan",
        BaichuanForCausalLM,
        False,
        lambda m: m.model.layers[0].self_attn.__class__,
        lambda m: m.model.layers[0].__class__,
    ),
    model_info(
        "chatglm",
        ChatGLMForConditionalGeneration,
        False,
        lambda m: m.transformer.encoder.layers[0].self_attention.__class__,
        lambda m: m.transformer.encoder.layers[0].__class__,
    ),
    model_info(
        "gptbigcode",
        transformers.models.gpt_bigcode.modeling_gpt_bigcode.GPTBigCodeForCausalLM,
        True,
        lambda m: m.transformer.h[0].attn.__class__,
        lambda m: m.transformer.h[0].__class__,
    ),
    model_info(
        "t5",
        transformers.models.t5.modeling_t5.T5ForConditionalGeneration,
        False,
        lambda m: m.decoder.block[0].layer[0].SelfAttention.__class__,
        lambda m: m.decoder.block[0].__class__,
    ),
    model_info(
        "mistral",
        transformers.models.mistral.modeling_mistral.MistralForCausalLM,
        True,
        lambda m: m.model.layers[0].self_attn.__class__,
        lambda m: m.model.layers[0].__class__,
    ),
    model_info(
        "mpt",
        transformers.models.mpt.modeling_mpt.MptForCausalLM,
        False,
        lambda m: m.transformer.blocks[0].attn.__class__,
        lambda m: m.transformer.blocks[0].__class__,
    ),
    model_info(
        "mixtral",
        transformers.models.mixtral.modeling_mixtral.MixtralForCausalLM,
        True,
        lambda m: m.model.layers[0].self_attn.__class__,
        lambda m: m.model.layers[0].__class__,
    ),
    model_info(
        "stablelm",
        transformers.models.stablelm.modeling_stablelm.StableLmForCausalLM,
        True,
        lambda m: m.model.layers[0].self_attn.__class__,
        lambda m: m.model.layers[0].__class__,
    ),
    model_info(
        "qwen",
        QWenLMHeadModel,
        False,
        lambda m: m.transformer.h[0].attn.__class__,
        lambda m: m.transformer.h[0].__class__,
    ),
    model_info(
        "qwen2",
        transformers.models.qwen2.modeling_qwen2.Qwen2ForCausalLM,
        True,
        lambda m: m.model.layers[0].self_attn.__class__,
        lambda m: m.model.layers[0].__class__,
    ),
    model_info(
        "git",
        transformers.models.git.modeling_git.GitForCausalLM,
        False,
        lambda m: m.git.encoder.layer[0].attention.self.__class__,
        lambda m: m.git.encoder.layer[0].__class__,
    ),
    model_info(
        "llava",
        LlavaLlamaForCausalLM,
        False,
        lambda m: m.model.layers[0].self_attn.__class__,
        lambda m: m.model.layers[0].__class__,
    ),
    model_info(
        "yuan",
        YuanForCausalLM,
        False,
        lambda m: m.model.layers[0].self_attn.__class__,
        lambda m: m.model.layers[0].__class__,
    ),
    model_info(
        "phi",
        PhiForCausalLM,
        True,
        lambda m: m.model.layers[0].self_attn.__class__,
        lambda m: m.model.layers[0].__class__,
    ),
    model_info(
        "phi3",
        Phi3ForCausalLM,
        True,
        lambda m: m.model.layers[0].self_attn.__class__,
        lambda m: m.model.layers[0].__class__,
    ),
    model_info(
        "whisper",
        transformers.models.whisper.modeling_whisper.WhisperForConditionalGeneration,
        False,
        lambda m: m.model.decoder.layers[0].self_attn.__class__,
        lambda m: m.model.decoder.layers[0].__class__,
    ),
    model_info(
        "llama3",
        transformers.models.llama.modeling_llama.LlamaForCausalLM,
        False,
        lambda m: m.model.layers[0].self_attn.__class__,
        lambda m: m.model.layers[0].__class__,
    ),
    model_info(
        "mllama",
        transformers.models.mllama.modeling_mllama.MllamaForConditionalGeneration,
        True,
        lambda m: m.language_model.model.layers[0].self_attn.__class__,
        lambda m: m.language_model.model.layers[0].__class__,
    ),
    model_info(
        "maira2",
        Maira2ForConditionalGeneration,
        True,
        lambda m: m.language_model.model.layers[0].self_attn.__class__,
        lambda m: m.language_model.model.layers[0].__class__,
    ),
]


class OptimizeTransformersNightlyTester(TestCase):
    def model_replacement_check(
        self, m, dtype, deployment_mode, torchcompile=False, return_dict=False
    ):
        config = AutoConfig.from_pretrained(
            f"{curpath}/hf_configs/{m.name}",
            return_dict=return_dict,
            trust_remote_code=True,
        )
        model = m.model_class(config).eval()
        if m.name == "falcon":
            with torch.no_grad():
                ipex.nn.utils._model_convert.replace_customized_linear_with_linear(
                    model.eval()
                )
        elif m.name == "chatglm":
            state_dict = model.state_dict()
            for weight in [
                "transformer.encoder.layers.0.input_layernorm.weight",
                "transformer.encoder.layers.0.post_attention_layernorm.weight",
                "transformer.encoder.final_layernorm.weight",
            ]:
                state_dict[weight] = torch.rand(state_dict[weight].shape)
            model.load_state_dict(state_dict)
        elif m.name == "baichuan":
            state_dict = model.state_dict()
            for weight in [
                "model.layers.0.input_layernorm.weight",
                "model.layers.0.post_attention_layernorm.weight",
                "model.norm.weight",
            ]:
                state_dict[weight] = torch.rand(state_dict[weight].shape)
            model.load_state_dict(state_dict)
        elif m.name == "llava":
            model.get_vision_tower().load_model()
        model.eval()
        ref_m = copy.deepcopy(model)
        ipex_m = copy.deepcopy(model)
        ipex_m = ipex.llm.optimize(
            ipex_m, dtype=dtype, deployment_mode=deployment_mode, inplace=True
        )
        if torchcompile:
            torch._dynamo.reset()
            ipex._set_compiler_backend("inductor")
            ipex_m = torch.compile(ipex_m, backend="ipex")

        assert (
            m.attention_class(ipex_m)
            is ipex.transformers.models.cpu.modules.attentions._IPEXAttentionCPU
        )
        assert (
            m.decoder_class(ipex_m)
            is ipex.transformers.models.cpu.modules.decoder._IPEXDecoderLayerCPU
            if m.decoder_class is not None
            else True
        )

        input_ids = torch.ones(10).to(torch.long)
        attention_mask = torch.ones(len(input_ids))
        position_ids = torch.arange(len(input_ids))
        decoder_input_ids = torch.ones(1).to(torch.long)
        input_dict = {
            "input_ids": input_ids.unsqueeze(0),
            "attention_mask": attention_mask.unsqueeze(0),
            "use_cache": True,
        }
        if m.has_position_ids:
            input_dict["position_ids"] = position_ids.unsqueeze(0)
        if re.search("t5", model.config.architectures[0], re.IGNORECASE):
            input_dict["decoder_input_ids"] = decoder_input_ids.unsqueeze(0)
        if m.name == "git":
            input_dict["input_ids"] = torch.ones(1, 1).to(torch.long)
            input_dict["attention_mask"] = torch.ones(1, 1)
            input_dict["pixel_values"] = torch.zeros(1, 3, 224, 224)
        if m.name == "whisper":
            last_hidden_state = torch.rand([1, 32, 1280])
            input_dict = {
                "decoder_input_ids": torch.ones(4).to(torch.long).unsqueeze(0),
                "encoder_outputs": (last_hidden_state,),
            }
        if m.name == "mllama":
            cross_attention_mask = torch.ones(1, 10, 1, 4)
            pixel_values = torch.rand(
                1,
                1,
                4,
                3,
                560,
                560,
            )
            aspect_ratio_mask = torch.tensor([[[1, 1, 1, 1]]])
            aspect_ratio_ids = torch.tensor([[6]])
            input_dict["pixel_values"] = pixel_values
            input_dict["aspect_ratio_mask"] = aspect_ratio_mask
            input_dict["aspect_ratio_ids"] = aspect_ratio_ids
            input_dict["cross_attention_mask"] = cross_attention_mask
        if m.name == "maira2":
            input_ids = torch.ones(1448).to(torch.long).unsqueeze(0)
            input_ids[:, 31:1400] = model.config.image_token_index
            attention_mask = torch.ones_like(input_ids)
            position_ids = torch.arange(input_ids.shape[-1]).unsqueeze(0)
            pixel_values = torch.rand(1, 3, 518, 518)
            input_dict = {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "position_ids": position_ids,
                "pixel_values": pixel_values,
            }

        with torch.no_grad(), torch.cpu.amp.autocast(
            enabled=True if dtype in [torch.bfloat16, torch.float16] else False,
            dtype=dtype,
        ):
            key_hf = ref_m(**input_dict)
        with torch.no_grad(), torch.cpu.amp.autocast(
            enabled=True if dtype in [torch.bfloat16, torch.float16] else False,
            dtype=dtype,
        ):
            key_ipex = ipex_m(**input_dict)
        error_message = f"model={m.name}, deployment_mode={deployment_mode}, torchcompile={torchcompile}, return_dict={return_dict}"
        if m.name != "mllama":
            if return_dict:
                assert isinstance(key_ipex, dict)
                self.assertEqual(
                    key_hf["logits"],
                    key_ipex["logits"],
                    prec=0.1,
                    message=error_message,
                )
            else:
                assert isinstance(key_ipex, tuple)
                self.assertEqual(
                    key_hf[0], key_ipex[0], prec=0.1, message=error_message
                )

    def test_model_replacement(self):
        dtypes = [torch.bfloat16]
        if core.onednn_has_fp16_support():
            dtypes.append(torch.float16)
        enable_torchcompile = [False, True]
        deployment_mode = [True, False]
        return_dict = [False, True]
        for m, torchcompile, dtype, jit, return_dict in itertools.product(
            supported_models, enable_torchcompile, dtypes, deployment_mode, return_dict
        ):
            if torchcompile and deployment_mode:
                continue
            self.model_replacement_check(m, dtype, jit, torchcompile, return_dict)
        _disable_tpp()


if __name__ == "__main__":
    test = unittest.main()
