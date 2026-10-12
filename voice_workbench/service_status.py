"""Explain a service process failure using only its current startup log."""
import re


def startup_failure(output, port, returncode):
    lower = output.casefold()
    binding = "bind on address" in lower or "绑定端口" in lower
    if binding and any(marker in lower for marker in ("errno 13", "10013", "权限不允许", "permission denied")):
        title = f"端口 {port} 被系统拒绝"
        hint = (f"Windows 拒绝本地服务绑定 127.0.0.1:{port}。请从程序目录正常启动，"
                "或在限制解除后点击「重新连接」。也可用 --port 指定其他可用端口，并更新双语工作流接入链接。")
    elif binding and any(marker in lower for marker in ("errno 98", "errno 10048", "10048", "already in use", "通常每个套接字")):
        title = f"端口 {port} 已被占用"
        hint = "请关闭占用该端口的其他程序后重新连接，或使用 --port 指定其他端口。"
    elif "此数据目录已有语音服务" in output:
        title = "该数据目录已有服务"
        hint = "请连接原服务所在端口，或先正常退出原来的声笺及独立服务后重新连接。"
    elif "permissionerror" in lower:
        title = "数据目录访问被拒绝"
        hint = "请确认数据目录可读写，并从程序目录正常启动声笺。原数据库和音色无需删除。"
    else:
        title = f"服务启动失败 · 退出码 {returncode}"
        hint = "可点击「重新连接」重试；完整原因见下方服务日志。"
    errors = [line.strip() for line in output.splitlines()
              if re.search(r"(?:ERROR:|\w+(?:Error|Exception):)", line)]
    detail = hint + ("\n\n" + "\n".join(errors[-4:]) if errors else "")
    return title, detail
