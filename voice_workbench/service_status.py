"""Explain a service process failure using only its current startup log."""
import re


def port_conflict(occupant, port):
    """Explain a port that could not be cleared before the service starts."""
    if occupant.pid is None:
        return (f"端口 {port} 被系统保留",
                "该端口上没有进程，是 Windows 为 Hyper-V/WinNAT 预留了这段端口，结束进程也无济于事。\n"
                "以管理员身份执行 netsh int ipv4 add excludedportrange protocol=tcp "
                f"startport={port} number=1 可把它排除，或重启 winnat 服务后再试。")
    if occupant.ours:
        return (f"端口 {port} 上的旧服务停不下来",
                f"{occupant.label} 既没有响应关闭请求，也没有被结束。\n"
                "请用任务管理器结束它，然后点击「重新连接」。")
    return (f"端口 {port} 被 {occupant.label} 占用",
            "这不是声笺的服务，程序不会替你结束它。请关闭该程序后点击「重新连接」。\n"
            f"可执行文件：{occupant.exe or '未知'}")


def startup_failure(output, port, returncode):
    lower = output.casefold()
    binding = "bind on address" in lower or "绑定端口" in lower
    if binding and any(marker in lower for marker in ("errno 13", "10013", "权限不允许", "permission denied")):
        title = f"端口 {port} 被系统拒绝"
        hint = (f"Windows 拒绝本地服务绑定 127.0.0.1:{port}。请从程序目录正常启动，"
                "或在限制解除后点击「重新连接」。也可用 --port 指定其他可用端口，并更新双语工作流接入链接。")
    elif binding and any(marker in lower for marker in ("errno 98", "errno 10048", "10048", "already in use", "通常每个套接字")):
        title = f"端口 {port} 已被占用"
        hint = ("启动瞬间端口又被抢占了。请点击「重新连接」重试；若反复出现，说明有程序会动态占用该端口，"
                "需要先关闭它。")
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
