# Copyright 2024 Bytedance Ltd. and/or its affiliates
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

import os
from typing import List, Union

import pandas as pd

import torch
from torch.utils.data import Dataset
from transformers import AutoTokenizer

from verl.utils import hf_tokenizer
from verl.utils.torch_functional import tokenize_and_postprocess_data


def download_files_distributed(download_fn):
    import torch.distributed
    if torch.distributed.is_initialized():
        if torch.distributed.get_rank() == 0:
            # download files
            download_fn()

        torch.distributed.barrier()
    else:
        # download anyway
        download_fn()


class RMDataset(Dataset):
    # TODO(zhangchi.usc1992): currently, we assume each prompt must contain N responses. Support setting a maximum N to enable
    # each prompt with less than N and more than N responses. (truncate for more than N and add dummy response if less than N)

    def __init__(self,
                 parquet_files: Union[str, List[str]],
                 tokenizer,
                 prompt_key='prompt',
                 responses_key='responses',
                 max_length=1024,
                 add_eos=True,
                 cache_dir='~/.cache/verl/rm'):
        if not isinstance(parquet_files, List):
            parquet_files = [parquet_files]

        self.parquet_files = parquet_files
        self.cache_dir = os.path.expanduser(cache_dir)
        if isinstance(tokenizer, str):
            tokenizer = hf_tokenizer(tokenizer)
        self.tokenizer = tokenizer

        self.prompt_key = prompt_key
        self.responses_key = responses_key

        self.add_eos = add_eos
        self.max_length = max_length

        self._download()
        self._read_files_and_tokenize()

    def _download(self):

        def _download_files():
            from verl.utils.fs import copy, _is_non_local
            os.makedirs(self.cache_dir, exist_ok=True)
            assert os.path.exists(self.cache_dir)
            for i, parquet_file in enumerate(self.parquet_files):
                if _is_non_local(parquet_file):
                    dst = os.path.join(self.cache_dir, os.path.basename(parquet_file))
                    if not os.path.exists(dst):
                        copy(src=parquet_file, dst=dst)
                    self.parquet_files[i] = dst

        download_files_distributed(_download_files)

    def _read_files_and_tokenize(self):
        dataframes = []
        for parquet_file in self.parquet_files:
            # read parquet files and cache
            dataframe = pd.read_parquet(parquet_file)
            dataframes.append(dataframe)
        self.dataframe = pd.concat(dataframes)
        self.prompts = self.dataframe[self.prompt_key].tolist()
        self.responses = self.dataframe[self.responses_key].tolist()

    def __len__(self):
        return len(self.prompts)

    def _pad_to_length(self, input_ids, attention_mask):
        curr_length = input_ids.shape[-1]

        if curr_length < self.max_length:
            input_ids = torch.cat(
                (input_ids, torch.zeros(size=(self.max_length - curr_length,), dtype=input_ids.dtype)), dim=-1)
            attention_mask = torch.cat(
                (attention_mask, torch.zeros(size=(self.max_length - curr_length,), dtype=attention_mask.dtype)),
                dim=-1)
        elif curr_length > self.max_length:
            input_ids = input_ids[:self.max_length]
            attention_mask = attention_mask[:self.max_length]

        return input_ids, attention_mask

    def __getitem__(self, item):
        prompt = self.prompts[item]
        responses = self.responses[item]

        prompt_with_chat_template = self.tokenizer.apply_chat_template(prompt, add_generation_prompt=True, tokenize=False)
        input_ids_lst = []
        attention_mask_lst = []
        for response in responses:
            input_str = prompt_with_chat_template + response
            if self.add_eos:
                input_str = input_str + self.tokenizer.eos_token

            input_ids, attention_mask = tokenize_and_postprocess_data(input_str, tokenizer=self.tokenizer, max_length=self.max_length, 
                                                                      pad_token_id=self.tokenizer.pad_token_id, left_pad=False)
            
            input_ids_lst.append(input_ids[0])
            attention_mask_lst.append(attention_mask[0])

        input_ids = torch.stack(input_ids_lst, dim=0)
        attention_mask = torch.stack(attention_mask_lst, dim=0)

        return {
            'input_ids': input_ids,
            'attention_mask': attention_mask,
        }

if __name__ == '__main__':
    parquet_files = '/mnt/bn/seed-rlhf-hl/zhangchi.usc1992/data/gsm8k/Qwen2.5-3B-Instruct_output_after_ranking.parquet'
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained('/mnt/bn/seed-rlhf-hl/zhangchi.usc1992/models/Qwen2.5-3B-Instruct')
    dataset = RMDataset(parquet_files=parquet_files, tokenizer=tokenizer, prompt_key='prompt', responses_key='responses', max_length=2048)
    
    data_item = dataset[0]