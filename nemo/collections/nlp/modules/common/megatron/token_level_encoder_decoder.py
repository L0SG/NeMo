# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
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

import torch

from nemo.collections.nlp.modules.common.megatron.language_model import Embedding
from nemo.collections.nlp.modules.common.megatron.megatron_decoders import get_decoder_model
from nemo.collections.nlp.modules.common.megatron.megatron_encoder_decoder import (
    MegatronTransformerEncoderDecoderModule,
)
from nemo.collections.nlp.modules.common.megatron.megatron_encoders import get_encoder_model
from nemo.collections.nlp.modules.common.megatron.module import MegatronModule
from nemo.collections.nlp.modules.common.megatron.t5_relative_position_embedding import T5RelativePositionEmbedding
from nemo.collections.nlp.modules.common.megatron.utils import (
    ApexGuardDefaults,
    build_position_ids,
    init_method_normal,
    parallel_lm_logits,
    scaled_init_method_normal,
)

try:
    from apex.transformer import tensor_parallel, parallel_state
    from apex.transformer.enums import AttnMaskType, ModelType

    HAVE_APEX = True
except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False
    # fake missing classes with None attributes
    AttnMaskType = ApexGuardDefaults()
    ModelType = ApexGuardDefaults()

__all__ = ["MegatronTokenLevelHead", "MegatronTokenLevelEncoderDecoderModule"]


class MegatronTokenLevelHead(MegatronModule):
    """Masked LM head for token-based encoder-decoder models (e.g., T5)

    Arguments:
        mpu_vocab_size: model parallel size of vocabulary.
        parallel_output: wether output logits being distributed or not.
    """

    def __init__(self, mpu_vocab_size, parallel_output):
        super(MegatronTokenLevelHead, self).__init__()

        self.bias = torch.nn.Parameter(torch.zeros(mpu_vocab_size))
        self.bias.model_parallel = True
        self.bias.partition_dim = 0
        self.bias.stride = 1
        self.parallel_output = parallel_output

    def forward(self, hidden_states, word_embeddings_weight):

        async_tensor_model_parallel_allreduce = parallel_state.get_tensor_model_parallel_world_size() > 1
        output = parallel_lm_logits(
            hidden_states,
            word_embeddings_weight,
            self.parallel_output,
            bias=self.bias,
            async_tensor_model_parallel_allreduce=async_tensor_model_parallel_allreduce,
        )
        return output


# TODO: add soft prompts as an Embedding sub-class


class MegatronTokenLevelEncoderDecoderModule(MegatronModule):
    """Token-based (input/output is tokens) encoder-decoder model (e.g. T5 Language model.)"""

    def __init__(
        self,
        encoder_arch,
        decoder_arch,
        vocab_size,
        hidden_size,
        max_position_embeddings,
        num_layers,
        num_attention_heads,
        ffn_hidden_size,
        apply_query_key_layer_scaling=True,
        kv_channels=None,
        num_tokentypes=0,
        parallel_output=True,
        pre_process=True,
        post_process=True,
        init_method_std=0.02,
        fp16_cross_entropy=False,
        use_cpu_initialization=False,
        hidden_dropout=0.1,
        attention_dropout=0.1,
        position_embedding_type='learned_absolute',
        relative_attention_num_buckets=32,
        relative_attention_max_distance=128,
        relative_position_bias_self_attention_only=False,
        precision=16,
        fp32_residual_connection=False,
        activations_checkpoint_method=None,
        activations_checkpoint_num_layers=1,
        layernorm_epsilon=1e-5,
        persist_layer_norm=False,
        bias_activation_fusion=True,
        bias_dropout_add_fusion=True,
        masked_softmax_fusion=True,
        openai_gelu=False,
        activation='gelu',
        onnx_safe=False,
        bias=True,
        normalization='layernorm',
        transformer_block_type='pre_ln',
        hidden_steps=-1,
        headscale=False,
        add_encoder=True,
        add_decoder=True,
        num_self_attention_per_cross_attention=1,
        share_token_embeddings=True,
        share_decoder_tokens_head_embeddings=True,
    ):
        super(MegatronTokenLevelEncoderDecoderModule, self).__init__()

        self.parallel_output = parallel_output
        self.pre_process = pre_process
        self.post_process = post_process
        self.fp16_cross_entropy = fp16_cross_entropy
        self.precision = precision
        self.add_encoder = add_encoder
        self.add_decoder = add_decoder
        self.normalization = normalization
        self.position_embedding_type = position_embedding_type
        self.relative_attention_num_buckets = relative_attention_num_buckets
        self.relative_attention_max_distance = relative_attention_max_distance
        self.share_token_embeddings = share_token_embeddings
        self.share_decoder_tokens_head_embeddings = share_decoder_tokens_head_embeddings

        if self.position_embedding_type == 'learned_absolute':
            add_position_embedding = True
        elif self.position_embedding_type == 'relative':
            add_position_embedding = False
        else:
            raise ValueError('Unknown position embeeding type. Options: ' '[learned_absolute | relative]')
        self.relative_position_bias_self_attention_only = relative_position_bias_self_attention_only

        if kv_channels is None:
            assert (
                hidden_size % num_attention_heads == 0
            ), 'hidden_size must be divisible by num_attention_heads if kv_channels is None'
            kv_channels = hidden_size // num_attention_heads

        encoder, decoder = None, None
        if add_encoder:
            if pre_process:
                self.encoder_embedding = Embedding(
                    hidden_size=hidden_size,
                    vocab_size=vocab_size,
                    max_sequence_length=max_position_embeddings,
                    init_method=init_method_normal(init_method_std),
                    num_tokentypes=num_tokentypes,
                    use_cpu_initialization=use_cpu_initialization,
                    embedding_dropout_prob=hidden_dropout,
                    position_embedding_type=position_embedding_type,
                )
                self._encoder_embedding_key = "encoder_embedding"
                if self.position_embedding_type == 'relative':
                    self.encoder_relative_position_embedding = T5RelativePositionEmbedding(
                        init_method=init_method_normal(init_method_std),
                        num_attention_heads=num_attention_heads,
                        relative_position_num_buckets=relative_attention_num_buckets,
                        relative_position_max_distance=relative_attention_max_distance,
                        bidirectional=True,
                    )
                    self._encoder_relative_position_embedding_key = "encoder_relative_position_embedding"

            encoder = get_encoder_model(
                arch=encoder_arch,
                hidden_size=hidden_size,
                ffn_hidden_size=ffn_hidden_size,
                num_layers=num_layers,
                num_attention_heads=num_attention_heads,
                apply_query_key_layer_scaling=apply_query_key_layer_scaling,
                kv_channels=kv_channels,
                init_method=init_method_normal(init_method_std),
                scaled_init_method=scaled_init_method_normal(init_method_std, num_layers),
                encoder_attn_mask_type=AttnMaskType.padding,
                pre_process=pre_process,
                post_process=post_process,
                init_method_std=init_method_std,
                use_cpu_initialization=use_cpu_initialization,
                hidden_dropout=hidden_dropout,
                attention_dropout=attention_dropout,
                precision=precision,
                fp32_residual_connection=fp32_residual_connection,
                activations_checkpoint_method=activations_checkpoint_method,
                activations_checkpoint_num_layers=activations_checkpoint_num_layers,
                layernorm_epsilon=layernorm_epsilon,
                bias_activation_fusion=bias_activation_fusion,
                bias_dropout_add_fusion=bias_dropout_add_fusion,
                masked_softmax_fusion=masked_softmax_fusion,
                persist_layer_norm=persist_layer_norm,
                openai_gelu=openai_gelu,
                onnx_safe=onnx_safe,
                hidden_steps=hidden_steps,
                activation=activation,
                bias=bias,
                normalization=normalization,
                transformer_block_type=transformer_block_type,
                headscale=headscale,
                parent_model_type=ModelType.encoder_and_decoder,
                num_self_attention_per_cross_attention=num_self_attention_per_cross_attention,
            )

        if add_decoder:
            # If this is the decoder first stage
            if pre_process:
                # If the encoder also lies on this rank (PP = 1), then just assign embeddings directly.
                if hasattr(self, 'encoder_embedding') and share_token_embeddings:
                    self.decoder_embedding = self.encoder_embedding
                else:
                    # This is the case where PP > 1 and first decoder first stage, or when not sharing embeddings with encoder
                    self.decoder_embedding = Embedding(
                        hidden_size=hidden_size,
                        vocab_size=vocab_size,
                        max_sequence_length=max_position_embeddings,
                        init_method=init_method_normal(init_method_std),
                        num_tokentypes=num_tokentypes,
                        use_cpu_initialization=use_cpu_initialization,
                        embedding_dropout_prob=hidden_dropout,
                        position_embedding_type=position_embedding_type,
                    )
                    # We initialize decoder embeddings, but set them to zero since we they're tied with the encoder embeddings.
                    # A later initialize_embedding call will synchronize the embeddings.
                    if share_token_embeddings:
                        self.decoder_embedding.zero_parameters()

                self._decoder_embedding_key = "decoder_embedding"
                # TODO (sandeepsub): When implementing RPE for PP > 2, this should not be inside `pre_process`. It should exist on all ranks and be synchronized manually.
                if self.position_embedding_type == 'relative':
                    self.decoder_relative_position_embedding = T5RelativePositionEmbedding(
                        init_method=init_method_normal(init_method_std),
                        num_attention_heads=num_attention_heads,
                        relative_position_num_buckets=relative_attention_num_buckets,
                        relative_position_max_distance=relative_attention_max_distance,
                        bidirectional=False,
                    )
                    self._decoder_relative_position_embedding_key = "decoder_relative_position_embedding"
                    if not self.relative_position_bias_self_attention_only:
                        self.decoder_cross_attention_relative_position_embedding = T5RelativePositionEmbedding(
                            init_method=init_method_normal(init_method_std),
                            num_attention_heads=num_attention_heads,
                            relative_position_num_buckets=relative_attention_num_buckets,
                            relative_position_max_distance=relative_attention_max_distance,
                            bidirectional=True,
                        )
                        self._decoder_cross_attention_relative_position_embedding_key = (
                            "decoder_cross_attention_relative_position_embedding"
                        )

            decoder = get_decoder_model(
                arch=decoder_arch,
                hidden_size=hidden_size,
                ffn_hidden_size=ffn_hidden_size,
                num_layers=num_layers,
                num_attention_heads=num_attention_heads,
                apply_query_key_layer_scaling=apply_query_key_layer_scaling,
                kv_channels=kv_channels,
                init_method=init_method_normal(init_method_std),
                scaled_init_method=scaled_init_method_normal(init_method_std, num_layers),
                decoder_attn_mask_type=AttnMaskType.causal,
                pre_process=pre_process,
                post_process=post_process,
                init_method_std=init_method_std,
                use_cpu_initialization=use_cpu_initialization,
                hidden_dropout=hidden_dropout,
                attention_dropout=attention_dropout,
                precision=precision,
                fp32_residual_connection=fp32_residual_connection,
                activations_checkpoint_method=activations_checkpoint_method,
                activations_checkpoint_num_layers=activations_checkpoint_num_layers,
                layernorm_epsilon=layernorm_epsilon,
                bias_activation_fusion=bias_activation_fusion,
                bias_dropout_add_fusion=bias_dropout_add_fusion,
                masked_softmax_fusion=masked_softmax_fusion,
                persist_layer_norm=persist_layer_norm,
                openai_gelu=openai_gelu,
                onnx_safe=onnx_safe,
                hidden_steps=hidden_steps,
                activation=activation,
                bias=bias,
                normalization=normalization,
                transformer_block_type=transformer_block_type,
                headscale=headscale,
                parent_model_type=ModelType.encoder_and_decoder,
            )

        self.enc_dec_model = MegatronTransformerEncoderDecoderModule(
            encoder=encoder, decoder=decoder, hidden_steps=hidden_steps
        )
        self._enc_dec_model_key = "enc_dec_model"

        # NOTE: Update here when adding support in PP > 1 (function has to be called)
        if self.share_token_embeddings:
            self.initialize_word_embeddings(
                init_method=init_method_normal(init_method_std), vocab_size=vocab_size, hidden_size=hidden_size
            )

        if add_decoder and post_process:
            if share_decoder_tokens_head_embeddings:
                self.tokens_head = MegatronTokenLevelHead(self.word_embeddings_weight().size(0), parallel_output)
            else:
                self.tokens_head = tensor_parallel.ColumnParallelLinear(
                    input_size=hidden_size,
                    output_size=vocab_size,
                    bias=False,
                    gather_output=not self.parallel_output,
                    init_method=init_method_normal(init_method_std),
                    use_cpu_initialization=use_cpu_initialization,
                )

            self._tokens_head_key = 'tokens_head'

    def set_input_tensor(self, input_tensor):
        """ See megatron.model.transformer.set_input_tensor()"""
        # This is usually handled in schedules.py but some inference code still
        # gives us non-lists or None

        if not isinstance(input_tensor, list):
            input_tensor = [input_tensor]

        if self.add_encoder and self.add_decoder:
            assert (
                len(input_tensor) == 1
            ), 'input_tensor should only be length 1 for stage with both encoder and decoder'
            self.enc_dec_model.encoder.set_input_tensor(input_tensor[0])
        elif self.add_encoder:
            assert len(input_tensor) == 1, 'input_tensor should only be length 1 for stage with only encoder'
            self.enc_dec_model.encoder.set_input_tensor(input_tensor[0])
        elif self.add_decoder:
            if len(input_tensor) == 2:
                self.enc_dec_model.decoder.set_input_tensor(input_tensor[0])
                self.enc_dec_model.encoder_hidden_state = input_tensor[1]
            elif len(input_tensor) == 1:
                self.enc_dec_model.decoder.set_input_tensor(None)
                self.enc_dec_model.encoder_hidden_state = input_tensor[0]
            else:
                raise Exception('input_tensor must have either length 1 or 2')
        else:
            raise Exception('Stage must have at least either encoder or decoder')

    def forward(
        self,
        enc_input_ids=None,
        enc_attn_mask=None,
        dec_input_ids=None,
        dec_attn_mask=None,
        token_type_ids=None,
        labels=None,
        enc_output=None,
        enc_output_attn_mask=None,
        enc_input=None,
        output_enc_hidden_only=False,
    ):
        """
        Return value is per token / per dimension (i.e., non collapsed loss value)
        """
        (
            encoder_self_attention_relative_position_bias,
            decoder_self_attention_relative_position_bias,
            decoder_cross_attention_relative_position_bias,
        ) = (None, None, None)

        enc_output_provided = enc_output is not None

        if (enc_input is None) and (enc_input_ids is not None):
            if self.pre_process and self.add_encoder:
                # We don't need position ids for RPE, because the embedding layer does not have position embeddings.
                if self.position_embedding_type != 'relative':
                    enc_position_ids = build_position_ids(enc_input_ids)
                else:
                    enc_position_ids = None
                enc_input = self.encoder_embedding(enc_input_ids, enc_position_ids, token_type_ids=token_type_ids)

                if self.position_embedding_type == 'relative':
                    encoder_self_attention_relative_position_bias = self.encoder_relative_position_embedding(
                        query_seq_length=enc_input_ids.size(1), key_seq_length=enc_input_ids.size(1),
                    )
            else:
                enc_input = None

        if output_enc_hidden_only:
            # When pipeline parallel > 1 we need to make sure encoder exist (will be missing in decoder)
            if enc_output is None and self.enc_dec_model.encoder is not None:
                enc_output = self.enc_dec_model.encode(
                    enc_input=enc_input, enc_attn_mask=enc_attn_mask, enc_layer_past=None, enc_get_key_value=False,
                )
            else:
                enc_output = self.enc_dec_model.encoder_hidden_state
            return enc_output
        else:
            if enc_output_attn_mask is None:
                enc_output_attn_mask = enc_attn_mask

            if self.pre_process and self.add_decoder:
                dec_position_ids = build_position_ids(dec_input_ids)
                dec_input = self.decoder_embedding(dec_input_ids, dec_position_ids, token_type_ids=token_type_ids)

                if self.position_embedding_type == 'relative':
                    decoder_self_attention_relative_position_bias = self.decoder_relative_position_embedding(
                        query_seq_length=dec_input_ids.size(1), key_seq_length=dec_input_ids.size(1)
                    )
                    if not self.relative_position_bias_self_attention_only:
                        decoder_cross_attention_relative_position_bias = self.decoder_cross_attention_relative_position_embedding(
                            query_seq_length=dec_input_ids.size(1), key_seq_length=enc_input_ids.size(1),
                        )
                    else:
                        decoder_cross_attention_relative_position_bias = None
            else:
                # Note: This is when the decoder itself is split across PP ranks.
                dec_input = None

            # If enc_output is provided in `batch_for_pipeline`, we need to transpose it from [B x S x H] -> [S x B x H].
            if enc_output_provided:
                enc_output = enc_output.transpose(0, 1)

            output = self.enc_dec_model(
                enc_input=enc_input,
                enc_attn_mask=enc_attn_mask,
                dec_input=dec_input,
                dec_attn_mask=dec_attn_mask,
                enc_layer_past=None,
                enc_get_key_value=False,
                enc_output=enc_output,
                enc_output_attn_mask=enc_output_attn_mask,
                dec_layer_past=None,
                dec_get_key_value=False,
                enc_self_attention_relative_position_bias=encoder_self_attention_relative_position_bias,
                dec_self_attention_relative_position_bias=decoder_self_attention_relative_position_bias,
                dec_cross_attention_relative_position_bias=decoder_cross_attention_relative_position_bias,
            )

            if self.post_process and self.add_decoder:
                dec_output, enc_output = output  # [s, b, h]
                # project decoder output to vocabulary-size dimensions
                if self.share_decoder_tokens_head_embeddings:
                    token_logits = self.tokens_head(dec_output, self.word_embeddings_weight())
                else:
                    token_logits = self.tokens_head(dec_output)[0]

                if labels is not None:
                    # [b, s] -> [s, b]
                    labels = labels.transpose(0, 1).contiguous()

                    # tensor_parallel.vocab_parallel_cross_entropy performs log_softmax and return log p(x_i|z) per token i
                    if self.fp16_cross_entropy:
                        assert token_logits.dtype == torch.half
                        tokens_loss = tensor_parallel.vocab_parallel_cross_entropy(token_logits, labels)
                    else:
                        tokens_loss = tensor_parallel.vocab_parallel_cross_entropy(token_logits.float(), labels)

                    # [s, b] -> [b, s]
                    tokens_loss = tokens_loss.transpose(0, 1).contiguous()

                    return tokens_loss
                else:
                    # [s, b, h] -> [b, s, h]
                    token_logits = token_logits.transpose(0, 1).contiguous()
                    return token_logits

            elif self.add_decoder and not self.add_encoder:
                decoder_output, _ = output
                return decoder_output
            else:
                encoder_output = output
                return encoder_output

    def state_dict_for_save_checkpoint(self, destination=None, prefix='', keep_vars=False):
        """For easy load when model is combined with other heads,
        add an extra key."""

        state_dict_ = {}

        state_dict_[self._encoder_embedding_key] = self.encoder_embedding.state_dict_for_save_checkpoint(
            destination, prefix, keep_vars
        )
        state_dict_[self._decoder_embedding_key] = self.decoder_embedding.state_dict_for_save_checkpoint(
            destination, prefix, keep_vars
        )
        state_dict_[self._enc_dec_model_key] = self.enc_dec_model.state_dict_for_save_checkpoint(
            destination, prefix, keep_vars
        )
        state_dict_[self._tokens_head_key] = self.tokens_head.state_dict_for_save_checkpoint(
            destination, prefix, keep_vars
        )

        return state_dict_

    def load_state_dict(self, state_dict, strict=True):
        """Customized load."""

        self.encoder_embedding.encoder_embeddingload_state_dict(state_dict[self._encoder_embedding_key], strict=strict)
        self.decoder_embedding.load_state_dict(state_dict[self._decoder_embedding_key], strict=strict)
        self.enc_dec_model.load_state_dict(state_dict[self._enc_dec_model_key], strict=strict)
        self.tokens_head.load_state_dict(state_dict[self._tokens_head_key], strict=strict)
