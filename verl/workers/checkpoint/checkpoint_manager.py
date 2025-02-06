import os
import shutil
from filelock import FileLock
import tempfile

import torch
import torch.distributed
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP, StateDictType
from transformers import PreTrainedTokenizer
import numpy as np
import random


class BaseCheckpointManager:
    """
    A checkpoint manager that saves and loads
    - model
    - optimizer
    - lr_scheduler
    - extra_states
    in a SPMD way.

    We save
    - sharded model states and optimizer states
    - full lr_scheduler states
    - huggingface tokenizer and config for ckpt merge
    """

    def __init__(self, model: FSDP, optimizer: torch.optim.Optimizer,
                 lr_scheduler: torch.optim.lr_scheduler.LRScheduler, tokenizer: PreTrainedTokenizer):
        self.previous_global_step = None
        self.previous_save_local_path = None

        self.model = model
        self.optimizer = optimizer
        self.lr_scheduler = lr_scheduler
        self.tokenizer = tokenizer

        assert isinstance(self.model, FSDP)
        self.rank = torch.distributed.get_rank()

    def load_checkpoint(self, *args, **kwargs):
        raise NotImplementedError

    def save_checkpoint(self, *args, **kwargs):
        raise NotImplementedError

    def remove_previous_save_local_path(self):
        if not self.previous_save_local_path:
            return

        abs_path = os.path.abspath(self.previous_save_local_path)
        print(f'Checkpoint manager remove previous save local path: {abs_path}')
        if not os.path.exists(abs_path):
            return

        # remove previous local_path
        shutil.rmtree(abs_path, ignore_errors=True)

    @staticmethod
    def local_mkdir(path, use_temp_dir=False, is_abs=True):
        """
        Create a directory with proper path handling.
        
        Args:
            path: Directory path to create
            use_temp_dir: If True, create directory in system temp directory
            is_abs: If True, treat path as absolute path
        """
        if use_temp_dir:
            path = os.path.join(tempfile.gettempdir(), path + '.lock')
        elif is_abs:
            path = path
        else:
            package_dir = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(package_dir, path)
        with FileLock(path):
            # make a new dir
            os.makedirs(path, exist_ok=True)
        
        return path

    @staticmethod
    def get_rng_state():
        rng_state = {
            'cpu': torch.get_rng_state(),
            'cuda': torch.cuda.get_rng_state(),
            'numpy': np.random.get_state(),
            'random': random.getstate(),
        }
        return rng_state

    @staticmethod
    def load_rng_state(rng_state):
        torch.set_rng_state(rng_state['cpu'])
        torch.cuda.set_rng_state(rng_state['cuda'])
        np.random.set_state(rng_state['numpy'])
        random.setstate(rng_state['random'])
