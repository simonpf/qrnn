"""
quantnn.models.pytorch.ligthning
================================

Interface for PyTorch lightning.
"""
import sys
import pickle


import numpy as np
import pytorch_lightning as pl
import torch
from torch import nn
from pytorch_lightning.utilities.rank_zero import rank_zero_only


from quantnn.packed_tensor import PackedTensor


def combine_outputs_list(y_preds, ys):
    y_pred_c = []
    y_c = []
    for y_pred, y in zip(y_preds, ys):
        comb = combine_outputs(y_pred, y)
        if comb is None:
            continue
        y_pred, y = comb
        y_pred_c.append(y_pred)
        y_c.append(y)
    if len(y_pred_c) == 0:
        return None
    return y_pred_c, y_c

def combine_outputs_dict(y_preds, ys):
    y_pred_c = {}
    y_c = {}
    for k_y_pred in y_preds:
        k_y = k_y_pred.split("/")[-1]
        comb = combine_outputs(y_preds[k_y_pred], ys[k_y])
        if comb is None:
            continue
        y_pred, y = comb
        y_pred_c[k_y_pred] = y_pred
        y_c[k_y] = y
    if len(y_pred_c) == 0:
        return None
    return y_pred_c, y_c


def combine_outputs(y_pred, y):
    """
    Combine potentially sparse retrieval outputs.

    Args:
         y_pred: List, dict, tensor or packed tensor of predicted output.
         y: List, dict or tensor containing reference outputs

    Return:
         The output with only
    """
    if isinstance(y_pred, list):
        return combine_outputs_list(y_pred, y)

    if isinstance(y_pred, dict):
        return combine_outputs_dict(y_pred, y)

    if isinstance(y_pred, PackedTensor):
        if isinstance(y, PackedTensor):
            y_pred_v, y_v = y_pred.intersection(y_pred, y)
            if y_pred_v is None:
                return None
            return y_pred_v._t, y_v._t
        else:
            if len(y_pred.batch_indices) == 0:
                return None
            return y_pred._t, y[y_pred.batch_indices]
    else:
        if isinstance(y, PackedTensor):
            if len(y.batch_indices) == 0:
                return None
            return y_pred[y.batch_indices], y._t
    return y_pred, y


def to_device(x, device=None, dtype=None):
    if isinstance(x, tuple):
        return tuple([to_device(x_i, device=device, dtype=dtype) for x_i in x])
    elif isinstance(x, list):
        return [to_device(x_i, device=device, dtype=dtype) for x_i in x]
    elif isinstance(x, dict):
        return {k: to_device(x_i, device=device, dtype=dtype) for k, x_i in x.items()}
    elif isinstance(x, PackedTensor):
        return x.to(device=device, dtype=dtype)
    return x.to(device=device, dtype=dtype)


class QuantnnLightning(pl.LightningModule):
    """
    Pytorch Lightning module for quantnn pytorch models.
    """
    def __init__(
            self,
            qrnn,
            loss,
            name=None,
            optimizer=None,
            scheduler=None,
            metrics=None,
            mask=None,
            transformation=None,
            log_dir=None
    ):
        super().__init__()
        self.validation_step_outputs = []
        self.qrnn = qrnn
        self.model = qrnn.model
        self.loss = loss
        self._stage = 0
        self._stage_name = None

        self.optimizer = optimizer
        self.current_optimizer = None
        self.scheduler = scheduler

        self.metrics = metrics
        if self.metrics is None:
            self.metrics = []
        for metric in self.metrics:
            metric.model = self.qrnn
            metric.mask = mask

        self.transformation = transformation


        if log_dir is None:
            log_dir = "lightning_logs"
        self.log_dir = log_dir
        self.name = name
        self._tensorboard = None


    @property
    def tensorboard(self):
        if self._tensorboard is None:
            self._tensorboard = pl.loggers.TensorBoardLogger(
                self.log_dir,
                name=self.name + f" ({self.stage_name})"
            )
        return self._tensorboard

    @property
    def stage(self):
        return self._stage

    @stage.setter
    def stage(self, stage):
        self._stage = stage

    @property
    def stage_name(self):
        """Name of the stage used for logging."""
        if self._stage_name is None:
            return f"Stage {self.stage}"
        return self._stage_name

    @stage_name.setter
    def stage_name(self, new_name):
        self._stage_name = new_name


    def training_step(self, batch, batch_idx):
        x, y = batch
        #x = to_device(x, device=self.device, dtype=self.dtype)
        y_pred = self.model(x)

        y_pred, y = combine_outputs(y_pred, y)

        avg_loss, tot_loss, losses, n_samples = self.model._train_step(
            y_pred, y, self.loss, None,
            metrics=None,
            transformation=self.transformation
        )

        #x = x.detach().cpu().numpy()
        #if isinstance(x, list):
        #    x = [x_i.detach().cpu().numpy() for x_i in x]
        #elif isinstance(x, dict):
        #    x = {k: x_k.detach().cpu().numpy() for k, x_k in x.items()}
        #else:
        #    x = x.detach().cpu().numpy()

        #if isinstance(y, list):
        #    y = [y_i.detach().cpu().numpy() for y_i in y]
        #elif isinstance(y, dict):
        #    y = {k: y_k.detach().cpu().numpy() for k, y_k in y.items()}
        #else:
        #    y = y.detach().cpu().numpy()

        if np.isnan(avg_loss.detach().cpu().numpy()):
            if hasattr(self, "x_prev"):
                with open("x_prev.pckl", "wb") as output:
                    pickle.dump(self.x_prev, output)
                with open("y_prev.pckl", "wb") as output:
                    pickle.dump(self.y_prev, output)
            with open("x.pckl", "wb") as output:
                pickle.dump(x, output)
            with open("y.pckl", "wb") as output:
                pickle.dump(y, output)
            sys.exit()
        self.x_prev = x
        self.y_prev = y

        self.log(
            "Training loss",
            avg_loss,
            on_epoch=True,
            batch_size=n_samples,
            sync_dist=True
        )
        losses = {f"Training loss ({key})": loss for key, loss in losses.items()}
        self.log_dict(
            losses,
            on_epoch=True,
            batch_size=n_samples,
            sync_dist=True
        )
        return avg_loss


    def validation_step(self, batch, batch_idx):
        x, y = batch
        #x = to_device(x, device=self.device, dtype=self.dtype)
        y_pred = self.model(x)
        try:
            y_pred, y = combine_outputs(y_pred, y)
        except TypeError:
            return None

        avg_loss, tot_loss, losses, n_samples = self.model._train_step(
            y_pred, y, self.loss, None, metrics=self.metrics,
            transformation=self.transformation
        )
        self.log(
            "Validation loss",
            avg_loss,
            on_epoch=True,
            batch_size=n_samples,
            rank_zero_only=True
        )
        losses = {f"Validation loss ({key})": loss for key, loss in losses.items()}
        self.log_dict(
            losses,
            on_epoch=True,
            batch_size=n_samples,
            rank_zero_only=True
        )

        self.log(
            "Learning rate",
            self.current_optimizer.param_groups[0]["lr"],
        )

    def on_validation_epoch_start(self):
        for metric in self.metrics:
            metric.reset()

    @rank_zero_only
    def on_validation_epoch_end(self):

        validation_step_output = self.validation_step_outputs


        i_epoch = self.trainer.current_epoch
        writer = self.tensorboard.experiment

        #if self.trainer.is_global_zero:

        figures = {}
        values = {}

        for metric in self.metrics:
            # Log values.
            if hasattr(metric, "get_values"):
                m_values = metric.get_values()
                if isinstance(m_values, dict):
                    m_values = {
                        f"{metric.name} ({key})": value
                        for key, value in m_values.items()
                    }
                else:
                    m_values = {metric.name: m_values}

                values.update(m_values)

            # Log figures.
            if hasattr(metric, "get_figures"):
                m_figures = metric.get_figures()
                if isinstance(m_figures, dict):
                    m_figures = {
                        f"{metric.name} ({key})": value
                        for key, value in m_figures.items()
                    }
                else:
                    m_figures = {metric.name: m_figures}
                figures.update(m_figures)


        for key, value in values.items():
            if isinstance(value, np.ndarray):
                values[key] = value.item()


        log_scalar = writer.add_scalar
        for key, value in values.items():
            log_scalar(key, value, i_epoch)

        log_image = writer.add_figure
        for key, value in figures.items():
            log_image(key, value, i_epoch)


    def configure_optimizers(self):

        staged = False
        if isinstance(self.optimizer, list) or isinstace(self.scheduler, list):
            staged = True

        if self.optimizer is None:
            optimizer = torch.optim.Adam(
                self.model.parameters(),
                lr=1e-3
            )
        else:
            if staged and isinstance(self.optimizer, list):
                optimizer = self.optimizer[self.stage]
            else:
                optimizer = self.optimizer

        conf = {
            "optimizer": optimizer
        }
        self.current_optimizer = optimizer

        if self.scheduler is None:
            return conf

        if staged and isinstance(self.scheduler, list):
            scheduler = self.scheduler[self.stage]
        else:
            scheduler = self.scheduler

        scheduler_config = {
            "scheduler": scheduler,
            "monitor": "Validation loss",
            "interval": "epoch",
            "frequency": 1,
            "strict": True,
            "name": "learning_rate",
        }
        if hasattr(scheduler, "stepwise"):
            if scheduler.stepwise:
                scheduler_config["interval"] = "step"

        conf["lr_scheduler"] = scheduler_config
        return conf

    def on_fit_end(self):
        self._tensorboard = None
        self.stage += 1

    def on_save_checkpoint(self, checkpoint) -> None:
        """
        Hook used to store 'stage' attribute in checkpoint.
        """
        checkpoint["stage"] = self.stage

    def on_load_checkpoint(self, checkpoint) -> None:
        """
        Hook used load store 'stage' attribute from checkpoint.
        """
        self.stage = checkpoint["stage"]
