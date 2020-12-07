import os

from typing import List, Optional

import torch
import torch.distributed as torch_distrib
from torch import nn
from torch.nn.parallel import DistributedDataParallel

from pytorch_lightning import LightningModule
from pytorch_lightning import _logger as log
from pytorch_lightning.plugins.rpc_plugin import RPCPlugin
from pytorch_lightning.utilities import FAIRSCALE_PIPE_AVAILABLE
from pytorch_lightning.utilities.exceptions import MisconfigurationException

if FAIRSCALE_PIPE_AVAILABLE:
    import fairscale.nn.model_parallel as mpu
    from fairscale.nn import PipeRPCWrapper
    from fairscale.nn.model_parallel.utils import ensure_divisibility
    from fairscale.nn.pipe import balance as pipe_balance
    from fairscale.nn.pipe import rpc as rpc_pipe
    from fairscale.nn.pipe.pipeline import PipelineStyle
    from torch.distributed import rpc


class PipeRPCPlugin(RPCPlugin):
    def __init__(self,
                 balance: Optional[List[int]] = None,
                 num_partitions: Optional[int] = None,
                 microbatches: int = 8,
                 checkpoint: str = 'except_last',
                 balance_mode: str = "balance_by_size",
                 pipelined_backward: Optional[bool] = True,
                 **kwargs):
        self._check_pipe_available()
        super().__init__(**kwargs)

        self.balance = balance
        if self.balance is None:
            raise MisconfigurationException(
                'Please, provide a balance for your model. '
                'Example: nn.Sequential(torch.nn.Linear(32, 32), nn.ReLU(), nn.Linear(32, 2)) contains 3 layers. '
                'A possible balance between 2 gpus is [2, 1]'
            )

        self.num_partitions = num_partitions
        self.microbatches = microbatches
        self.checkpoint = checkpoint
        self.balance_mode = balance_mode
        self.pipelined_backward = pipelined_backward
        self.main_rpc_process = False  # Updated by main process, default for all secondary processes

    def init_distributed_connection(
            self,
            trainer,
            cluster_environment,
            global_rank: int,
            world_size: int,
            is_slurm_managing_tasks: bool = True,
    ) -> None:
        self._check_manual_optimization(trainer)
        if not self._skip_init_connections(trainer):
            super().init_distributed_connection(
                trainer=trainer,
                cluster_environment=cluster_environment,
                global_rank=global_rank,
                world_size=world_size,
                is_slurm_managing_tasks=is_slurm_managing_tasks
            )
            super().init_rpc_connection(
                global_rank=global_rank,
                world_size=world_size
            )
            self.init_model_parallel_groups()

            if self.main_rpc_process:
                self._check_sequential_model_exists(trainer)
                if self.balance is None:
                    self.balance = self._infer_model_balance(trainer)
                self._assert_valid_model_balance(trainer)
        self.set_main_rpc_process()

    def _infer_model_balance(self, trainer):
        model = trainer.get_model()
        partitions = torch.cuda.device_count() if self.num_partitions is None else self.num_partitions
        if model.example_input_array is None:
            raise MisconfigurationException(
                'Please set example_input_array to your model, so we can infer the right model balance for you')
        balance_func = getattr(pipe_balance, self.balance_mode)
        log.info(f'The following model balance {self.balance} was inferred using {self.balance_mode} mode')
        return balance_func(partitions, model.layers, model.example_input_array)

    def _check_sequential_model_exists(self, trainer):
        model = trainer.get_model()
        if not hasattr(model, "layers") or not isinstance(model.layers, nn.Sequential):
            raise MisconfigurationException(
                'Could not find a PipeLightningModule within the model. '
                'Did you defined set your sequential model as an `layers` attribute of your model ?')

    def _find_pipe_module(self, model):
        found_module = False
        if hasattr(model, "layers") and isinstance(model.layers, LightningPipeModule):
            # model has been wrapped already
            found_module = True
        elif hasattr(model, "layers") and isinstance(model.layers, nn.Sequential):
            # try to wrap model for the user
            model.layers = LightningPipeModule(
                model.layers,
                balance=self.balance,
                microbatches=self.microbatches,
                checkpoint=self.checkpoint,
            )
            model.final_stage = model.layers.module.final_stage
            model.foreach_worker = model.layers.module.foreach_worker
            model.layers.module.model.trainer = model.trainer
            model.layers.module.model.configure_optimizers = model.configure_optimizers
            found_module = True

        if not found_module:
            raise MisconfigurationException(
                'Could not find a PipeLightningModule within the model. '
                'Did you defined set your sequential model as an `layers` attribute of your model ?')

    def _assert_valid_model_balance(self, trainer):
        model = trainer.get_model()
        if sum(self.balance) != len(model.layers):
            raise MisconfigurationException(
                f'The provided balance sum: {sum(self.balance)} doesn t match your Sequential length: {len(model.layers)}')

    def _skip_init_connections(self, trainer):
        """
        Skip initialization if torch is already initialized and we're in testing.
        Returns: Whether to skip initialization

        """
        if torch_distrib.is_initialized() and trainer.testing:
            return True
        return False

    def init_model_parallel_groups(self):
        self.num_gpus_per_model = len(self.balance)
        num_model_parallel = 1  # TODO currently no support for vertical model parallel
        mpu.initialize_model_parallel(
            model_parallel_size_=num_model_parallel,
            pipeline_length=self.num_gpus_per_model
        )

    def on_exit_rpc_process(self, trainer):
        if not trainer.testing:
            torch_distrib.barrier()  # Ensure we await main process initialization

            # Add trainer/configure_optimizers to the pipe model for access in all worker processes
            rpc_pipe.PipeModel.trainer = trainer
            rpc_pipe.PipeModel.configure_optimizers = trainer.model.configure_optimizers
        # For RPC, all ranks other than 0 just need to call rpc.shutdown()
        torch.distributed.rpc.shutdown()

    def set_main_rpc_process(self):
        self.main_rpc_process = torch_distrib.get_rank(group=mpu.get_pipeline_parallel_group()) == 0

    def on_main_rpc_connection(self, trainer):
        # Create pipe_module
        model = trainer.get_model()
        self._find_pipe_module(model)
        if not trainer.testing:
            torch_distrib.barrier()  # Ensure we join main process initialization
            model.foreach_worker(register_optimizers, include_self=True)

    def _check_manual_optimization(self, trainer):
        automatic_optimization = trainer.train_loop.automatic_optimization
        if automatic_optimization:
            raise MisconfigurationException(
                'PipeRPCPlugin is currently not supported in automatic optimization')

        if trainer.amp_backend is not None:
            raise MisconfigurationException(
                'PipeRPCPlugin is currently not supported in Automatic Mixed Precision')

    def on_after_setup_optimizers(self, trainer):
        self._optimizers_map = {opt_idx: False for opt_idx, opt in enumerate(trainer.optimizers)}

    def configure_ddp(
            self, model: LightningModule, device_ids: List[int]
    ) -> DistributedDataParallel:
        ddp_plugin = RPCPlugin(process_group=mpu.get_data_parallel_group()).configure_ddp(model, device_ids)
        return ddp_plugin

    def rpc_save_model(self, save_model_fn, last_filepath, trainer, pl_module):
        model = trainer.get_model()
        if hasattr(model, "foreach_worker"):
            current_layers = pl_module.layers
            model.foreach_worker(
                save_layers_on_all_rank_zero_workers,
                {"num_gpus_per_model": self.num_gpus_per_model},
                include_self=True
            )
            pl_module.layers = reload_sequential_from_saved_layers(self.num_gpus_per_model)
            save_model_fn(last_filepath, trainer, pl_module)
            del pl_module.layers
            pl_module.layers = current_layers

    def _optimizer_step(self, model, opt_idx, *args, **kwargs):
        model.foreach_worker(run_optimizer, {"opt_idx": opt_idx}, include_self=False)

    def optimizer_step(self,
                       model,
                       lightning_optimizer,
                       closure,
                       *args,
                       **kwargs):
        opt_idx = lightning_optimizer._optimizer_idx
        self._optimizers_map[opt_idx] = not self._optimizers_map[opt_idx]

        if self._optimizers_map[opt_idx]:
            lightning_optimizer.step(closure=closure, *args, **kwargs)
            self._optimizer_step(model, opt_idx, *args, **kwargs)
            return True
        return False

    def distributed_sampler_kwargs(self, distributed_sampler_kwargs):
        distributed_sampler_kwargs = dict(
            num_replicas=len(mpu.get_model_parallel_group()),
            rank=torch_distrib.get_rank() % len(self.balance),
        )
        return distributed_sampler_kwargs

    @property
    def data_parallel_group(self) -> torch_distrib.group:
        return mpu.get_data_parallel_group()

    @property
    def is_main_rpc_process(self):
        return self.main_rpc_process

    def _check_pipe_available(self):
        if not FAIRSCALE_PIPE_AVAILABLE:
            raise MisconfigurationException(
                'PipeRPCPlugin requires FairScale and currently is only supported on PyTorch 1.6.'
            )


class LightningPipeModule(nn.Module):
    """
        This class wraps Fairscale Pipe and PipeRCPWrapper class.

        Args:
            module: nn.Sequential
                sequential model to be balanced among several gpus

            balance: list of ints
                list of number of layers in each partition.

            checkpoint (str) = 'never'
                when to enable checkpointing, one of ``'always'``,
                ``'except_last'``, or ``'never'`` (default: ``'except_last'``)

            balance_mode: str = "balance_by_size"
                when balance is not provided, the model can be balanced either by size or time.
                refer to balance description.

            mode: PipeMode
                the mode enables switching between Pipe and PipeRCPWrapper class
    """

    def __init__(self,
                 module: nn.Sequential,
                 balance: List[int],
                 microbatches: int = 8,
                 checkpoint='never'):
        super().__init__()
        self.module = module
        self.balance = balance
        self.microbatches = microbatches
        self.checkpoint = checkpoint
        self._init_pipe()

    def _init_pipe(self):
        device = torch.device("cuda", torch_distrib.get_rank())

        self.module = PipeRPCWrapper(
            module=self.module,
            balance=self.balance,
            chunks=self.microbatches,
            style=PipelineStyle.MultiProcess,
            input_device=device,
            worker_map=self.get_worker_map(),
            checkpoint=self.checkpoint,
        )

    def forward(self, *args, **kwargs):
        x = self.module(*args, **kwargs)
        return x

    def get_worker_map(self):
        # TODO, is this correct with multinodes? We also assume "worker" is the same as defined in the RPCPlugin
        return {rank: f"worker{rank}" for rank in range(torch_distrib.get_world_size())}


def register_optimizers(ctx, model):
    optimizers, lr_schedulers, optimizer_frequencies = model.trainer.init_optimizers(model)
    model.trainer.optimizers = optimizers
    model.trainer.lr_schedulers = lr_schedulers
    model.trainer.optimizer_frequencies = optimizer_frequencies
    model.trainer.convert_to_lightning_optimizers()


def do_nothing_optimizer_closure():
    return


def run_optimizer(ctx, model):
    trainer = model.trainer
    opt_idx = ctx["opt_idx"]
    optimizer = trainer.optimizers[opt_idx]
    closure = getattr(optimizer, "_closure", do_nothing_optimizer_closure)
    optimizer.step(closure=closure)


def save_layers_on_all_rank_zero_workers(ctx, model):
    num_gpus_per_model = ctx["num_gpus_per_model"]
    rank = torch_distrib.get_rank()
    if rank in range(num_gpus_per_model):
        seq = list(model.children())[0]
        torch.save(seq, f"seq_{rank}.pt")


def reload_sequential_from_saved_layers(num_gpus_per_model):
    partial_seqs = [torch.load(f"seq_{rank}.pt", map_location='cpu') for rank in range(num_gpus_per_model)]
    seq = nn.Sequential()
    for p_seq in partial_seqs:
        for name, child in p_seq.named_children():
            seq.add_module(name, child)
    # delete tmp files
    _ = [os.remove(f"seq_{rank}.pt") for rank in range(num_gpus_per_model)]
    return seq