import asyncio
import fcntl
import json
import os
import pty
import select
import signal
import struct
import termios

MOUNT_PATH = "/vol"


def add_terminal_routes(api_app, touch=None) -> None:
    from fastapi import HTTPException, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, Response

    def touch_request() -> None:
        if touch:
            try:
                touch()
            except Exception:
                pass

    @api_app.get("/terminal")
    def serve_terminal():
        for p in [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "terminal.html"),
            "/app/terminal.html",
        ]:
            if os.path.exists(p):
                with open(p, "r") as f:
                    return HTMLResponse(content=f.read())
        raise HTTPException(status_code=404, detail="terminal.html not found")

    @api_app.get("/favicon.ico")
    def serve_favicon():
        svg = (
            "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"64\" height=\"64\" "
            "viewBox=\"0 0 24 24\">"
            "<circle cx=\"12\" cy=\"12\" r=\"12\" fill=\"#111827\"/>"
            "<path fill=\"#ffffff\" d=\"M17 3.34a10 10 0 1 1 -14.995 8.984l-.005 -.324l.005 "
            "-.324a10 10 0 0 1 14.995 -8.336zm-1.293 5.953a1 1 0 0 0 -1.32 -.083l-.094 "
            ".083l-3.293 3.292l-1.293 -1.292l-.094 -.083a1 1 0 0 0 -1.403 1.403l.083 .094l2 "
            "2l.094 .083a1 1 0 0 0 1.226 0l.094 -.083l4 -4l.083 -.094a1 1 0 0 0 -.083 "
            "-1.32z\"/>"
            "</svg>"
        )
        return Response(content=svg, media_type="image/svg+xml")

    @api_app.websocket("/ws/terminal")
    async def terminal_ws(websocket: WebSocket):
        await websocket.accept()
        touch_request()

        pid, fd = pty.fork()
        if pid == 0:
            # Child process
            # Prefer opening in the media directory inside the volume.
            media_dir = os.path.join(MOUNT_PATH, "media")
            if os.path.exists(media_dir):
                os.chdir(media_dir)
            else:
                os.chdir(MOUNT_PATH if os.path.exists(MOUNT_PATH) else "/")

            env = os.environ.copy()
            env["PATH"] = f"/root/.local/bin:{env.get('PATH', '')}"
            env["TERM"] = "xterm-256color"
            env["COLORTERM"] = "truecolor"
            env["LANG"] = os.environ.get("LANG", "en_US.UTF-8")
            os.execvpe("/bin/bash", ["/bin/bash"], env)

        # Parent process: make PTY reads non-blocking so we can batch output.
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except Exception:
            pass

        async def read_pty():
            loop = asyncio.get_running_loop()
            buf = bytearray()
            last_send = loop.time()
            max_frame_interval = 1.0 / 60.0
            max_batch_bytes = 64 * 1024

            async def flush(force: bool = False) -> None:
                nonlocal last_send
                if not buf:
                    return
                now = loop.time()
                if (
                    not force
                    and (now - last_send) < max_frame_interval
                    and len(buf) < max_batch_bytes
                ):
                    return
                try:
                    payload = buf.decode("utf-8", errors="replace")
                except Exception:
                    payload = ""
                buf.clear()
                last_send = now
                if payload:
                    await websocket.send_json({"type": "output", "data": payload})

            try:
                while True:
                    await asyncio.sleep(0.005)

                    if select.select([fd], [], [], 0)[0]:
                        while True:
                            try:
                                data = os.read(fd, 65536)
                            except BlockingIOError:
                                break
                            except OSError:
                                await flush(force=True)
                                return

                            if not data:
                                await flush(force=True)
                                return

                            buf.extend(data)
                            if len(buf) >= max_batch_bytes:
                                break

                    await flush(force=False)
            except Exception:
                pass
            finally:
                try:
                    await flush(force=True)
                except Exception:
                    pass

        read_task = asyncio.create_task(read_pty())

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type")
                if msg_type == "input":
                    data = msg.get("data", "")
                    if data:
                        touch_request()
                        os.write(fd, data.encode("utf-8"))
                elif msg_type == "resize":
                    cols = max(1, min(500, int(msg.get("cols", 80))))
                    rows = max(1, min(200, int(msg.get("rows", 24))))
                    winsize = struct.pack("HHHH", rows, cols, 0, 0)
                    touch_request()
                    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
                    # Some TUIs only redraw on SIGWINCH.
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGWINCH)
                    except Exception:
                        try:
                            os.kill(pid, signal.SIGWINCH)
                        except Exception:
                            pass
                elif msg_type == "ping":
                    touch_request()
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            read_task.cancel()
            try:
                await read_task
            except Exception:
                pass

            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(10):
                    try:
                        waited_pid, _ = os.waitpid(pid, os.WNOHANG)
                        if waited_pid != 0:
                            break
                    except ChildProcessError:
                        break
                    await asyncio.sleep(0.05)
                else:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            except Exception:
                pass

            try:
                os.close(fd)
            except Exception:
                pass
