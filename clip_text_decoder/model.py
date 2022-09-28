from __future__ import annotations

import os
import tempfile
from typing import Callable, List, Optional, Tuple, Union

import clip
import gdown
import torch
import torch.nn.functional as F
from clip.model import CLIP
from PIL import Image
from pytorch_lightning import LightningModule
from torch import Tensor, nn, optim
from transformers import GPT2Config, GPT2LMHeadModel, GPT2Tokenizer

PRETRAINED_INFERENCE_MODEL_PATH = (
    "https://drive.google.com/uc?id=1oXPhrXMqRO_Q1UFe4NAs_RvDXR1AGoL2"
    # https://drive.google.com/file/d/1oXPhrXMqRO_Q1UFe4NAs_RvDXR1AGoL2/view?usp=sharing
)


class ClipDecoder(LightningModule):
    def __init__(self, gpt2_type: str = "distilgpt2"):
        super().__init__()
        self.config = GPT2Config.from_pretrained(gpt2_type, add_cross_attention=True)
        self.gpt = GPT2LMHeadModel.from_pretrained(gpt2_type, config=self.config)

    def forward(
        self,
        input_ids: Tensor,
        encoder_hidden_states: Tensor,
        attention_mask: Optional[Tensor] = None,
        labels: Optional[Tensor] = None,
    ):
        batch_size, _, num_features = encoder_hidden_states.shape
        # TODO: Check if we can get '768' (num_features) from the GPT2 model.
        hidden = torch.zeros(
            size=(batch_size, 1, 768),
            dtype=encoder_hidden_states.dtype,
            device=encoder_hidden_states.device,
        )
        hidden[:, :, :num_features] = encoder_hidden_states

        return self.gpt(
            input_ids=input_ids,
            encoder_hidden_states=hidden,
            attention_mask=attention_mask,
            labels=labels,
        )

    def configure_optimizers(self):
        return optim.AdamW(self.parameters(), lr=1e-4, betas=(0.9, 0.98))

    def training_step(self, batch: Tuple[Tensor, Tensor, Tensor], *_) -> Tensor:
        encoder_hidden_states, input_ids, attention_mask = batch
        result = self.forward(
            input_ids=input_ids,
            encoder_hidden_states=encoder_hidden_states,
            attention_mask=attention_mask,
            labels=input_ids,
        )

        self.log("training_loss", result.loss, on_step=False, on_epoch=True)
        return result.loss

    @torch.no_grad()
    def validation_step(self, batch: Tuple[Tensor, Tensor, Tensor], *_) -> Tensor:
        encoder_hidden_states, input_ids, attention_mask = batch
        result = self.forward(
            input_ids=input_ids,
            encoder_hidden_states=encoder_hidden_states,
            attention_mask=attention_mask,
            labels=input_ids,
        )

        self.log("validation_loss", result.loss, on_step=False, on_epoch=True)
        return result.loss


class ClipDecoderInferenceModel:
    _model_path = "model.pt"
    _tokenizer_path = "tokenizer.pkl"

    def __init__(
        self,
        model: ClipDecoder,
        tokenizer: GPT2Tokenizer,
    ):
        self.model = model.eval()
        self.tokenizer = tokenizer

    @property
    def device(self) -> torch.device:
        return next(self.model.parameters()).device

    def to(self, device: torch.device) -> ClipDecoderInferenceModel:
        self.model.to(device)
        return self

    def save(self, path: str):
        # Save a copy of the current model weights, and cast to FP16 for storage
        model_state_dict = self.model.state_dict()
        # Avoid saving any cached properties of this class or its subclasses :)
        obj = self.__class__(model=self.model.half(), tokenizer=self.tokenizer)
        torch.save(obj, path)
        # Restore the original model weights
        self.model.load_state_dict(model_state_dict)

    @classmethod
    def load(cls, path: str) -> ClipDecoderInferenceModel:
        temp = torch.load(path)
        # Just in case we change any of the class methods here, unpack the model
        # and tokenizer, and pass them into a new instance of this class.
        return cls(model=temp.model.float(), tokenizer=temp.tokenizer)

    @classmethod
    def download_pretrained(cls, dest: str = None) -> ClipDecoderInferenceModel:
        with tempfile.TemporaryDirectory() as tempdir:
            if dest is None:
                dest = os.path.join(tempdir, "model.zip")
            gdown.download(PRETRAINED_INFERENCE_MODEL_PATH, dest, quiet=False)
            return cls.load(dest)

    @torch.cuda.amp.autocast()
    @torch.no_grad()
    def __call__(self, x: Tensor, max_len: int = 64, beam_size: int = 1) -> str:
        encoder_hidden_states = x.reshape(1, 1, -1).to(self.device)
        input_ids = [torch.tensor([self.tokenizer.bos_token_id], device=self.device)]
        beam_logprobs: Optional[List[float]] = None

        def _get_beam_outputs(_input_ids: Tensor) -> Tuple[List[Tensor], Tensor]:
            outputs = self.model(_input_ids.unsqueeze(0), encoder_hidden_states)
            logits: Tensor = outputs.logits[0, -1]
            logprobs = F.log_softmax(logits, dim=-1)

            topk_logprobs = logprobs.topk(k=beam_size)
            indices = topk_logprobs.indices
            logprobs = topk_logprobs.values
            output_ids = [
                torch.cat([_input_ids, idx.reshape(-1)], dim=0) for idx in indices
            ]

            return output_ids, logprobs

        for _ in range(max_len - 1):
            output_ids: List[Tensor] = []
            logprobs: List[float] = []
            beams_done: List[bool] = []

            for beam_idx, ids in enumerate(input_ids):
                if beam_logprobs and ids[-1].item() == self.tokenizer.eos_token_id:
                    output_ids.append(ids)
                    logprobs.append(beam_logprobs[beam_idx])
                    beams_done.append(True)
                    continue

                _output_ids, _logprobs = _get_beam_outputs(ids)
                if beam_logprobs is not None:
                    _logprobs += beam_logprobs[beam_idx]
                output_ids += _output_ids
                logprobs += _logprobs.tolist()
                beams_done.append(False)

            if all(beams_done):
                # All search beams are done generating text.
                break

            indices = torch.tensor(logprobs).topk(k=beam_size).indices
            input_ids = [output_ids[idx] for idx in indices]
            beam_logprobs = [logprobs[idx] for idx in indices]

        best_beam_idx: int = torch.tensor(beam_logprobs).argmax().item()

        return self.tokenizer.decode(input_ids[best_beam_idx], skip_special_tokens=True)


class ImageCaptionInferenceModel(ClipDecoderInferenceModel):
    def __init__(self, model: ClipDecoder, tokenizer: GPT2Tokenizer):
        super().__init__(model, tokenizer)
        self._clip_model: Optional[CLIP] = None
        self._clip_preprocessor: Optional[Callable] = None

    def _load_clip(self):
        self._clip_model, self._clip_preprocessor = clip.load(
            "ViT-B/32", device=self.device, jit=False
        )

    @property
    def clip_model(self) -> CLIP:
        if self._clip_model is None:
            self._load_clip()
        assert self._clip_model is not None, "Could not load CLIP model."
        return self._clip_model

    @property
    def clip_preprocessor(self) -> Callable:
        if self._clip_preprocessor is None:
            self._load_clip()
        assert self._clip_preprocessor is not None, "Could not load CLIP model."
        return self._clip_preprocessor

    @torch.cuda.amp.autocast()
    @torch.no_grad()
    def __call__(
        self,
        image: Union[str, Image.Image],
        max_len: int = 64,
        beam_size: int = 1,
    ) -> str:
        if isinstance(image, str):
            image = Image.open(image)

        preprocessed: Tensor = self.clip_preprocessor(image).to(self.device)
        encoded = self.clip_model.encode_image(preprocessed.unsqueeze(0))
        return super().__call__(encoded, max_len=max_len, beam_size=beam_size)


if __name__ == "__main__":
    from PIL import Image

    model = ImageCaptionInferenceModel.download_pretrained().to("cuda")
    image = Image.open("puppy.jpg")
    caption = model(image)
    print(caption)
