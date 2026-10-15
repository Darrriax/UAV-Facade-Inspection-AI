"""Bridge for checkpoints trained with the CUBIT-InSeg YOLOv9 fork.

The weights the CUBIT-InSeg team publish are YOLOv9-c segmentation models, but
they were trained with a *patched* ultralytics whose YOLOv9 blocks live under
``ultralytics.nn.extra_modules.block``. A ``.pt`` is not a weights-only file: it
is a pickled ``nn.Module`` graph, so unpickling needs that exact module path to
exist. Upstream ultralytics never had it (see ultralytics issue #9372 - the
namespace is a fork convention that many YOLOv9 projects copy), and its
remaining YOLOv9 blocks have since been renamed (``RepNCSP`` -> ``RepCSP``,
``RepNBottleneck`` -> ``RepBottleneck``) while ``RepConvN`` was dropped
altogether. Loading such a checkpoint on the installed ultralytics therefore
fails with::

    ModuleNotFoundError: No module named 'ultralytics.nn.extra_modules'

This module defines the five classes the checkpoint references and registers
them under the path the pickle records, so ``torch.load`` resolves them. It is a
*compatibility* layer, not a second implementation to train with: the blocks are
the legacy YOLOv9 definitions, reproduced here because the fork's own source
was never published (its repository ships the dataset, not the training code).
``__init__`` bodies are only ever used if someone builds a model with this
module directly - a checkpoint restores the state dict and calls ``forward``.

The import of ``torch`` is at module scope on purpose: this file belongs to the
worker image. ``app.services.inference`` imports it lazily, inside the same
``try`` that imports ultralytics, so the API image can still import the
inference module without the ML stack installed.
"""

from __future__ import annotations

import logging
import sys
import types

import torch
import torch.nn as nn

from ultralytics.nn.modules.conv import Conv

logger = logging.getLogger(__name__)

#: Path recorded in the checkpoint's pickle. Kept in one place: a typo here is
#: the difference between "the model loads" and the original ModuleNotFoundError.
PACKAGE = "ultralytics.nn.extra_modules"
BLOCK = f"{PACKAGE}.block"


class RepConvN(nn.Module):
    """Rep-style convolution: a 3x3 branch plus a 1x1 branch, summed.

    Both branches are ``Conv(..., act=False)`` so exactly one activation runs on
    the sum. ``bn``/``deploy`` belong to RepVGG's train-then-fuse trick and stay
    unused here - the checkpoints store no batch norm in this block, and
    ``forward`` reads the attribute unconditionally.
    """

    default_act = nn.SiLU()

    def __init__(
        self,
        c1: int,
        c2: int,
        k: int = 3,
        s: int = 1,
        p: int = 1,
        g: int = 1,
        d: int = 1,
        act: bool | nn.Module = True,
        bn: bool = False,
        deploy: bool = False,
    ) -> None:
        super().__init__()
        assert k == 3 and p == 1
        self.g = g
        self.c1 = c1
        self.c2 = c2
        self.act = (
            self.default_act if act is True else act if isinstance(act, nn.Module) else nn.Identity()
        )
        self.bn = None
        self.conv1 = Conv(c1, c2, k, s, p=p, g=g, act=False)
        self.conv2 = Conv(c1, c2, 1, s, p=(p - k // 2), g=g, act=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        identity = 0 if self.bn is None else self.bn(x)
        return self.act(self.conv1(x) + self.conv2(x) + identity)

    def forward_fuse(self, x: torch.Tensor) -> torch.Tensor:
        """Deployed form: the two branches were folded into ``conv1``."""
        return self.act(self.conv1(x))


class RepNBottleneck(nn.Module):
    """Residual bottleneck whose 3x3 branch is a :class:`RepConvN`."""

    def __init__(
        self,
        c1: int,
        c2: int,
        shortcut: bool = True,
        g: int = 1,
        k: tuple[int, int] = (3, 3),
        e: float = 0.5,
    ) -> None:
        super().__init__()
        c_ = int(c2 * e)
        self.cv1 = RepConvN(c1, c_, k[0], 1)
        self.cv2 = Conv(c_, c2, k[1], 1, g=g)
        self.add = shortcut and c1 == c2

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.cv2(self.cv1(x)) if self.add else self.cv2(self.cv1(x))


class RepNCSP(nn.Module):
    """CSP stack of :class:`RepNBottleneck` blocks.

    Two 1x1 projections split the input; only the second one goes through the
    stack, and the concatenation is projected back - the structure ELAN4 is
    built from.
    """

    def __init__(
        self,
        c1: int,
        c2: int,
        n: int = 1,
        shortcut: bool = True,
        g: int = 1,
        e: float = 0.5,
    ) -> None:
        super().__init__()
        c_ = int(c2 * e)
        self.cv1 = Conv(c1, c_, 1, 1)
        self.cv2 = Conv(c1, c_, 1, 1)
        self.cv3 = Conv(2 * c_, c2, 1)
        self.m = nn.Sequential(
            *(RepNBottleneck(c_, c_, shortcut, g, e=1.0) for _ in range(n))
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.cv3(torch.cat((self.m(self.cv1(x)), self.cv2(x)), 1))


class ADown(nn.Module):
    """Stride-2 downsample that keeps both an average- and a max-pooled path.

    Half the channels are average-pooled and convolved, half are max-pooled and
    convolved; the average path is the one that survives the odd-size padding.
    """

    def __init__(self, c1: int, c2: int) -> None:
        super().__init__()
        self.c = c2 // 2
        self.cv1 = Conv(c1 // 2, self.c, 3, 2, 1)
        self.cv2 = Conv(c1 // 2, self.c, 1, 1, 0)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = torch.nn.functional.avg_pool2d(x, 2, 1, 0, False, True)
        x1, x2 = x.chunk(2, 1)
        x1 = self.cv1(x1)
        x2 = torch.nn.functional.max_pool2d(x2, 3, 2, 1)
        x2 = self.cv2(x2)
        return torch.cat((x1, x2), 1)


class RepNCSPELAN4(nn.Module):
    """ELAN4 block: split, two CSP stages, concatenate, project."""

    def __init__(self, c1: int, c2: int, c3: int, c4: int, n: int = 1) -> None:
        super().__init__()
        self.c = c3 // 2
        self.cv1 = Conv(c1, c3, 1, 1)
        self.cv2 = nn.Sequential(RepNCSP(c3 // 2, c4, n), Conv(c4, c4, 3, 1))
        self.cv3 = nn.Sequential(RepNCSP(c4, c4, n), Conv(c4, c4, 3, 1))
        self.cv4 = Conv(c3 + (2 * c4), c2, 1, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        y = list(self.cv1(x).chunk(2, 1))
        y.extend(stage(y[-1]) for stage in (self.cv2, self.cv3))
        return self.cv4(torch.cat(y, 1))


#: What the fork's ``block`` module exposed. Names are the pickled ones, so this
#: mapping is also the record of what a legacy checkpoint can be built from.
LEGACY_BLOCK_CLASSES: dict[str, type[nn.Module]] = {
    "ADown": ADown,
    "RepConvN": RepConvN,
    "RepNBottleneck": RepNBottleneck,
    "RepNCSP": RepNCSP,
    "RepNCSPELAN4": RepNCSPELAN4,
}


def install() -> bool:
    """Register the legacy module path so a fork-trained checkpoint can load.

    Returns whether this call was the one that registered it, which is what
    makes the "already installed" case a no-op rather than an error. Every
    process needs it once: ``torch.load`` resolves the pickled classes through
    ``sys.modules``, and a Celery worker handles each task in a child process of
    its own.

    The entry is registered as a *package* (``__path__`` set) and attached to
    its ``ultralytics.nn`` parent, because pickle's ``find_class`` imports the
    module path first - a bare module object in ``sys.modules`` is not enough
    for every import machinery path.
    """
    if BLOCK in sys.modules:
        return False

    import ultralytics.nn  # parent package, so the attribute below has a home

    package = types.ModuleType(PACKAGE)
    package.__path__ = []  # type: ignore[attr-defined]
    block = types.ModuleType(BLOCK)
    for name, cls in LEGACY_BLOCK_CLASSES.items():
        setattr(block, name, cls)
    package.block = block  # type: ignore[attr-defined]

    sys.modules[PACKAGE] = package
    sys.modules[BLOCK] = block
    setattr(ultralytics.nn, "extra_modules", package)

    logger.info(
        "Registered the legacy YOLOv9 module path %s (blocks: %s) for fork-trained checkpoints",
        BLOCK,
        ", ".join(LEGACY_BLOCK_CLASSES),
    )
    return True


__all__ = [
    "ADown",
    "LEGACY_BLOCK_CLASSES",
    "RepConvN",
    "RepNBottleneck",
    "RepNCSP",
    "RepNCSPELAN4",
    "install",
]
