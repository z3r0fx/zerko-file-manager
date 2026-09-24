"""Compressed splat scenes (.spz, Niantic's open format) for the 3D viewer.

A scene of 3-4 million splats is a 700 MB+ .ply: every number a 32-bit float.
The .spz keeps the same splats in about a tenth of that: positions as 24-bit
fixed point, colour, opacity and size in a byte each, the turn in three bytes,
the view-dependent colour (spherical harmonics) in a byte per number, and the
whole thing gzipped. The viewer (Spark) reads it directly.

The values are written as they are in the .ply, with no change of axes, so
the scene sits exactly where the .ply put it.
"""
from __future__ import annotations

import gzip
import struct
from pathlib import Path

import numpy as np

MAGIC = 0x5053474E            # "NGSP"
VERSION = 2
COLOR_SCALE = 0.15
SH_C = {0: 0, 1: 3, 2: 8, 3: 15}


def _u8(x: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(x), 0, 255).astype(np.uint8)


def _quantize_sh(x: np.ndarray, bits: int) -> np.ndarray:
    bucket = 1 << (8 - bits)
    q = np.rint(x * 128.0 + 128.0).astype(np.int32)
    q = (q + bucket // 2) // bucket * bucket
    return np.clip(q, 0, 255).astype(np.uint8)


def encode(a: np.ndarray, fractional_bits: int = 12) -> bytes:
    """A structured array with the .ply's fields (x y z, f_dc_*, f_rest_*, opacity,
    scale_*, rot_*) -> the gzipped .spz bytes."""
    n = len(a)
    names = a.dtype.names or ()
    rest = sorted((k for k in names if k.startswith("f_rest_")), key=lambda k: int(k.split("_")[-1]))
    per = len(rest) // 3
    degree = next((d for d, c in SH_C.items() if c == per), 0)
    per = SH_C[degree]

    # positions: 24-bit signed fixed point, little endian, x y z per splat
    xyz = np.stack([a["x"], a["y"], a["z"]], -1).astype(np.float64)
    fixed = np.rint(xyz * (1 << fractional_bits)).astype(np.int64)
    fixed = np.clip(fixed, -(1 << 23), (1 << 23) - 1).astype(np.int32).reshape(-1)
    raw = fixed.view(np.uint8).reshape(-1, 4)[:, :3]           # little-endian low three bytes
    positions = np.ascontiguousarray(raw).tobytes()

    alphas = _u8(255.0 / (1.0 + np.exp(-a["opacity"].astype(np.float64)))).tobytes()

    dc = np.stack([a["f_dc_0"], a["f_dc_1"], a["f_dc_2"]], -1).astype(np.float64)
    colors = _u8(dc * (COLOR_SCALE * 255.0) + 0.5 * 255.0).tobytes()

    sc = np.stack([a["scale_0"], a["scale_1"], a["scale_2"]], -1).astype(np.float64)
    scales = _u8((sc + 10.0) * 16.0).tobytes()

    # rotation: the .ply has w x y z; spz v2 keeps x y z of the unit quaternion with w >= 0
    q = np.stack([a["rot_1"], a["rot_2"], a["rot_3"], a["rot_0"]], -1).astype(np.float64)
    q /= np.maximum(np.linalg.norm(q, axis=-1, keepdims=True), 1e-12)
    q *= np.where(q[:, 3:4] < 0, -1.0, 1.0)
    rotations = _u8(q[:, :3] * 127.5 + 127.5).tobytes()

    sh = b""
    if per:
        # .ply: all red coefficients, then green, then blue; spz: per coefficient r g b
        f = np.stack([a[k] for k in rest[:3 * per]], -1).astype(np.float64).reshape(n, 3, per)
        f = f.transpose(0, 2, 1)                                    # n, coefficient, channel
        out = np.empty(f.shape, np.uint8)
        out[:, :3] = _quantize_sh(f[:, :3], 5)
        out[:, 3:] = _quantize_sh(f[:, 3:], 4)
        sh = out.tobytes()

    head = struct.pack("<IIIBBBB", MAGIC, VERSION, n, degree, fractional_bits, 0, 0)
    return gzip.compress(head + positions + alphas + colors + scales + rotations + sh, compresslevel=6)


def ply_to_spz(ply: Path, out: Path) -> int:
    """Write out.spz next to the scene; returns its size in bytes."""
    import splat
    _head, a = splat._read_ply(ply)
    data = encode(a)
    tmp = out.with_suffix(".spz.tmp")
    tmp.write_bytes(data)
    tmp.replace(out)
    return len(data)


def decode(data: bytes) -> dict:
    """Read an .spz back (for checking): positions, alphas, colours, scales, rotations, sh."""
    b = gzip.decompress(data)
    magic, version, n, degree, fb, _flags, _r = struct.unpack_from("<IIIBBBB", b, 0)
    assert magic == MAGIC and version == 2
    o = 16
    p = np.frombuffer(b, np.uint8, n * 9, o).reshape(n * 3, 3).astype(np.int32)
    o += n * 9
    v = p[:, 0] | (p[:, 1] << 8) | (p[:, 2] << 16)
    v = np.where(v & 0x800000, v - (1 << 24), v)
    xyz = (v / float(1 << fb)).reshape(n, 3)
    alpha = np.frombuffer(b, np.uint8, n, o) / 255.0
    o += n
    col = (np.frombuffer(b, np.uint8, n * 3, o).reshape(n, 3) / 255.0 - 0.5) / COLOR_SCALE
    o += n * 3
    sc = np.frombuffer(b, np.uint8, n * 3, o).reshape(n, 3) / 16.0 - 10.0
    o += n * 3
    r = np.frombuffer(b, np.uint8, n * 3, o).reshape(n, 3) / 127.5 - 1.0
    o += n * 3
    w = np.sqrt(np.maximum(0.0, 1.0 - (r * r).sum(-1)))
    per = SH_C[degree]
    sh = (np.frombuffer(b, np.uint8, n * per * 3, o).reshape(n, per, 3) - 128.0) / 128.0 if per else None
    return {"xyz": xyz, "alpha": alpha, "color": col, "scale": sc, "rot_xyzw": np.column_stack([r, w]), "sh": sh}
