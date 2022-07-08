#!/usr/bin/env python3
# Realtime mesh analyzer and debug tool
#
# Copyright (C) 2022 Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations
import sys
import pathlib
import asyncio
import argparse
import math
import json
import time
import threading

have_scipy = True
try:
    from scipy.interpolate import RegularGridInterpolator
    import numpy as np
except ModuleNotFoundError:
    have_scipy = False

from typing import (
    Callable,
    Dict,
    List,
    Any,
    Optional,
)

UNIX_BUFFER_LIMIT = 20 * 1024 * 1024

# Linear interpolation between two values
def lerp(t, v0, v1):
    return (1. - t) * v0 + t * v1

class OutputHandler:
    def __init__(self, outfile: Optional[str] = None) -> None:
        self.loop = asyncio.get_event_loop()
        self.write_mutex = threading.Lock()
        self.file = None
        if outfile is not None:
            ofile = pathlib.Path(outfile)
            self.file = ofile.open("w")

    def write(self, message: str) -> asyncio.Future:
        return self.loop.run_in_executor(None, self._do_write, message)

    def _do_write(self, message: str):
        with self.write_mutex:
            msg = f"{message}\n"
            sys.stdout.write(msg)
            if self.file is not None:
                self.file.write(msg)
                self.file.flush()

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None

class KlippySocket:
    def __init__(
        self, out_hdlr: OutputHandler, on_disconnect: Callable
    ) -> None:
        self.loop = asyncio.get_event_loop()
        self.out_hdlr = out_hdlr
        self.on_disconnect: Callable = on_disconnect
        self.requests: Dict[int, asyncio.Future] = {}
        self.subscriptions: Dict[str, Callable] = {}
        self.read_task: Optional[asyncio.Task] = None
        self.connected = False

    async def connect(self, sockname: str) -> None:
        while 1:
            try:
                reader, writer = await asyncio.open_unix_connection(
                    sockname, limit=UNIX_BUFFER_LIMIT)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.)
            else:
                self.connected = True
                self.writer = writer
                loop = self.loop
                self.read_task = loop.create_task(self._read_socket(reader))
                return

    def send_request(
        self, endpoint: str, params: Dict[str, Any] = {}
    ) -> asyncio.Future:
        fut = self.loop.create_future()
        if not self.connected:
            fut.set_exception(Exception("Klippy not connected"))
            return fut
        uid = id(fut)
        self.requests[uid] = fut
        req = {"method": endpoint, "id": uid, "params": params}
        data = json.dumps(req).encode() + b"\x03"
        try:
            self.writer.write(data)
            # TODO: make this method async and call drain()?
        except Exception as e:
            self.out_hdlr.write(f"Error writing data: {e}")
            fut.set_exception(e)
            self.loop.create_task(self.close())
        return fut

    def register_subscription(
        self, endpoint: str, method: str, callback: Callable
    ) -> asyncio.Future:
        self.subscriptions[method] = callback
        return self.send_request(
            endpoint, {"response_template": {"method": method}}
        )

    async def _read_socket(self, reader: asyncio.StreamReader):
        errors_remaining: int = 10
        while not reader.at_eof():
            try:
                data = await reader.readuntil(b"\x03")
            except (ConnectionError, asyncio.IncompleteReadError):
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                errors_remaining -= 1
                if not errors_remaining or not self.connected:
                    break
                continue
            try:
                self._process_payload(json.loads(data[:-1]))
            except Exception as e:
                self.out_hdlr.write(f"Error processing payload: {e}")
        self.read_task = None
        await self.close()

    def _process_payload(self, payload: Dict[str, Any]):
        method = payload.get("method", None)
        if method is not None:
            # This is a remote method called from klippy
            if method in self.subscriptions:
                params = payload.get("params", {})
                ret = self.subscriptions[method](params)
                if ret is not None:
                    self.loop.create_task(ret)
            return
        # This is a response to a request, process
        request: Optional[asyncio.Future] = None
        if "id" in payload:
            request = self.requests.pop(payload["id"], None)
        if request is None:
            return
        if "result" in payload:
            request.set_result(payload["result"])
        else:
            errmsg = payload.get("error", "Malformed Klippy Response")
            request.set_exception(Exception(errmsg))

    async def close(self):
        if not self.connected:
            return
        self.connected = False
        if self.read_task is not None and not self.read_task.done():
            self.read_task.cancel()
            self.read_task = None
        await self.out_hdlr.write("Connection Closed")
        self.on_disconnect()


class BaseAnalyzer:
    def __init__(self, args: argparse.Namespace) -> None:
        self.run_task: Optional[asyncio.Task] = None
        self.out_hdlr = OutputHandler(args.outfile)
        self.ksock = KlippySocket(self.out_hdlr, self._on_disconnect)
        self.socketfile = args.socket

    def _on_disconnect(self):
        if self.run_task is not None and not self.run_task.done():
            self.run_task.cancel()

    async def start_analysis(self) -> None:
        await self.ksock.connect(self.socketfile)
        self.run_task = asyncio.get_event_loop().create_task(self._run())
        await self.run_task

    async def _run(self):
        raise NotImplementedError("Derived classes must implement _run()")

    async def close(self):
        await self.ksock.close()
        self.out_hdlr.close()


class MeshAnalyzer(BaseAnalyzer):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(args)
        self.mesh_min: List[float] = [0, 0.]
        self.mesh_max: List[float] = [0., 0.]
        self.points: List[List[float]] = []
        self.lift_distance: float = 0.
        self.lift_speed: float = 0.
        self.travel_speed: float = 60.

    async def _query_config(self) -> None:
        # Make sure bedmesh and either probe or bltouch
        # is in the config
        # Make sure fade is disabled

        pass

    async def _query_bedmesh(self) -> None:
        pass

    async def _query_toolhead(self) -> None:
        pass

    async def _probe_point(self) -> None:
        pass

    async def _reset_offsets(self) -> None:
        # Set x, y and z gcode offsets to 0
        # Same with G92
        # Clear skew
        pass

    async def _run(self):
        pass

class PrintAnalyzer(BaseAnalyzer):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(args)
        self.interp: Optional[RegularGridInterpolator] = None
        self.last_position: List[float] = [0., 0., 0., 0.]
        self.max_split: float = 0.
        self.mesh_offsets: List[float] = [0., 0.]
        self.mesh_min: List[float] = [0., 0.]
        self.mesh_max: List[float] = [0., 0.]
        self.fade_target: float = 0.
        self.adjusted_moves: int = 0
        self.total_splits: int = 0
        self.total_moves_recieved: int = 0
        self.bypassed_moves: int = 0
        self.small_segements: int = 0
        self.max_segment_length: float = 0.
        self.errors: int = 0
        self.max_zadj_diff: float = 0.

    def _event_position_reset(self, params: Dict[str, Any]) -> None:
        self.last_position[:] = params["last_position"]

    def _event_xy_offset_change(self, params: Dict[str, Any]) -> None:
        self.mesh_offsets = params["offsets"]
        self.out_hdlr.write(f"Updated mesh xy offsets: {self.mesh_offsets}")

    def _event_mesh_update(self, params: Dict[str, Any]) -> None:
        # Print current stats before reset
        self._print_stats()
        self.adjusted_moves = 0
        self.total_splits = 0
        self.total_moves_recieved = 0
        self.bypassed_moves = 0
        self.max_segment_length = 0.
        self.small_segements = 0
        self.errors = 0
        self.max_zadj_diff = 0.
        self.last_position[:] = params["last_position"]
        self.mesh_min = params["mesh_min"]
        self.mesh_max = params["mesh_max"]
        self.fade_target = params["fade_target"]
        matrix: List[List[float]] = params["mesh_matrix"]
        ycnt = len(matrix)
        xcnt = len(matrix[0])
        if xcnt == 0:
            self.max_split = 0.
            self.interp = None
            self.out_hdlr.write("Mesh Update: Mesh Cleared")
            return
        self.mesh_offsets = params["mesh_xy_offsets"]
        xdist = (self.mesh_max[0] - self.mesh_min[0]) / (xcnt - 1)
        ydist = (self.mesh_max[1] - self.mesh_min[1]) / (ycnt - 1)
        xcoords = [self.mesh_min[0] + i * xdist for i in range(xcnt)]
        ycoords = [self.mesh_min[1] + i * ydist for i in range(ycnt)]
        self.max_split = math.sqrt(xdist*xdist + ydist*ydist) + 1
        # As received, the axes are arranged as "matrix[Y][X]"  The
        # Grid Interpolator expects "matrix[X][Y]" so we need to transpose
        # the matrix
        nd_matrix = np.asarray(matrix).transpose()
        self.interp = RegularGridInterpolator(
            (xcoords, ycoords), nd_matrix, bounds_error=False, fill_value=None
        )
        str_matrix = "\n".join([repr(i) for i in matrix])
        self.out_hdlr.write(
            "Mesh Update: New Mesh Detected\n"
            f"Matrix:\n{str_matrix}\n"
            f"Mesh Min: {self.mesh_min}\n"
            f"Mesh Max: {self.mesh_max}\n"
            f"Fade Target: {self.fade_target}\n"
            f"Mesh XY Offsets: {self.mesh_offsets}\n"
            f"Last Position: {self.last_position}\n"
            f"Maximum Split Length: {self.max_split:.6f}"
        )

    def _lookup_zadj(self, pos: List[float], factor: float) -> float:
        # Add offsets to the position
        x = pos[0] + self.mesh_offsets[0]
        y = pos[1] + self.mesh_offsets[1]
        # Constrain the position within the mesh
        x = min(self.mesh_max[0], max(self.mesh_min[0], x))
        y = min(self.mesh_max[1], max(self.mesh_min[1], y))
        z = float(self.interp([x, y]))
        return factor * (z - self.fade_target) + self.fade_target

    def _get_segment_length(
        self, last_pos: List[float], pos: List[float]
    ) -> float:
        axes_d = [pos[i] - last_pos[i] for i in range(2)]
        return math.sqrt(sum([d*d for d in axes_d]))

    def _check_segment(
        self, last_pos: List[float],
        pos: List[float],
        check_pos: List[float],
        check_small: bool = False
    ) -> List[str]:
        errors: List[str] = []
        seg_len = self._get_segment_length(last_pos, pos)
        self.max_segment_length = max(self.max_segment_length, seg_len)
        if check_small and 1.0 > seg_len > 1e-6:
            self.small_segements += 1
        if seg_len > self.max_split:
            errors.append(
                f"Segment {pos}: segment length too long, "
                f"calculated: {seg_len}, max: {self.max_split}"
            )
        for (i, j, axis) in zip(check_pos, pos, "XYZE"):
            if axis == "Z":
                zdiff = abs(i - j)
                if zdiff > 1e-5:
                    # Difference greater than 10nm is above a rounding error
                    errors.append(
                        f"Segment {pos}: Unexpected difference in Z "
                        f"adjustment: {zdiff}"
                    )
                self.max_zadj_diff = max(self.max_zadj_diff, zdiff)
                continue
            if not math.isclose(i, j, abs_tol=1e-6):
                errors.append(
                    f"Segment {pos}: Position mismatch on {axis}: "
                    f"expected: {i}, received from bed mesh: {j}"
                )
        return errors

    def _event_move(self, params: Dict[str, Any]) -> None:
        self.total_moves_recieved += 1
        errors: List[str] = []
        newpos: List[float] = params["newpos"]
        if self.interp is not None:
            factor: float = params["factor"]
            adjusted: List[float] = params["adjusted"]
            if adjusted:
                adj_len = len(adjusted)
                self.adjusted_moves += adj_len
                self.total_splits += adj_len - 1
                last_pos = list(self.last_position)
                total_len = self._get_segment_length(last_pos, newpos)
                for pos in adjusted[:-1]:
                    t = (
                        self._get_segment_length(self.last_position, pos) /
                        total_len
                    )
                    check_pos = [
                        lerp(t, i, j) for (i, j) in
                        zip(self.last_position, newpos)
                    ]
                    check_pos[2] += self._lookup_zadj(pos, factor)
                    errors.extend(
                        self._check_segment(last_pos, pos, check_pos, True)
                    )
                    last_pos = pos
                # Validate the last segment
                final_pos = adjusted[-1]
                check_pos = list(newpos)
                check_pos[2] += self._lookup_zadj(final_pos, factor)
                errors.extend(
                    self._check_segment(last_pos, final_pos, check_pos)
                )
            else:
                self.bypassed_moves += 1
        else:
            self.bypassed_moves += 1
        if errors:
            self.errors += 1
            msg = f"Move Error from {self.last_position} - {newpos}\n"
            msg += "\n".join(errors)
            self.out_hdlr.write(msg)
        self.last_position[:] = newpos

    def _process_received(self, params: Dict[str, Any]):
        event = params.pop("event", "unknown")
        func: Callable = getattr(self, f"_event_{event}", None)
        if func is not None:
            func(params)

    async def _run(self):
        self.out_hdlr.write("Starting Print Analysis...")
        ret = await self.ksock.register_subscription(
            "bed_mesh/dump_mesh", "dump_mesh", self._process_received
        )
        self._event_mesh_update(ret)
        while self.ksock.connected:
            await asyncio.sleep(60.)
            self._print_stats()

    def _print_stats(self):
        if self.interp is None:
            return
        self.out_hdlr.write(
                f"Stats {time.asctime()}: "
                f"Received Moves: {self.total_moves_recieved} | "
                f"Bypassed Moves: {self.bypassed_moves} | "
                f"Splits Detected: {self.total_splits} | "
                f"Adjustments: {self.adjusted_moves} | "
                f"Errors Detected: {self.errors} | "
                f"Max Z-Adj Diff: {self.max_zadj_diff:.4e} | "
                f"Max Segment Length: {self.max_segment_length:.4f} | "
                f"Small Segments (< 1mm): {self.small_segements}"
            )


def main():
    parser = argparse.ArgumentParser(
        description="mesh_analysis - Bed Mesh Debugging Tool")
    parser.add_argument(
        "-o", "--outfile", default=None, metavar="<outfile>",
        help="Optional file path to write output"
    )
    parser.add_argument(
        "-a", "--analysis", default="mesh", choices=["mesh", "print"],
        metavar="<analysis type>",
        help="Type of analysis to perform"
    )
    parser.add_argument(
        "socket", metavar="<socket filename>",
        help="Path to Klippy's Unix Domain Socket")
    args = parser.parse_args()
    loop = asyncio.get_event_loop()
    exit_code = 0
    if args.analysis == "mesh":
        analyzer = MeshAnalyzer(args)
    elif not have_scipy:
        sys.stdout.write(
            "The 'scipy' and 'numpy' modules are required to perform\n"
            "a print analysis.  On debian based machines they can be\n"
            "installed with the following commands:\n\n"
            "sudo apt-get update\n"
            "sudo apt-get install python3-scipy\n"
        )
        sys.exit(1)
    else:
        analyzer = PrintAnalyzer(args)
    try:
        loop.run_until_complete(analyzer.start_analysis())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception as e:
        sys.stdout.write(f"Mesh analyzer exited with error: {e}\n")
        exit_code = 1
    finally:
        loop.run_until_complete(analyzer.close())
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
