"""统一错误码。"""


class AppError(Exception):
    def __init__(self, code: str, message: str, hint: str = "", status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.status = status

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "hint": self.hint}


class Cancelled(AppError):
    def __init__(self):
        super().__init__("CANCELLED", "任务已取消")


class RemoteUnavailable(AppError):
    def __init__(self, message: str):
        super().__init__("REMOTE_UNAVAILABLE", message, "检查 AutoDL 实例是否开机、SSH 隧道是否已建立", 503)


COOKIE_HINT = "请在「设置」里配置 B 站 Cookie（SESSDATA）后重试"


def map_ytdlp_error(e: Exception) -> AppError:
    """把 yt-dlp 的报错按关键字映射成错误码。"""
    msg = str(e)
    low = msg.lower()
    if "412" in msg or "precondition failed" in low:
        return AppError("RISK_CONTROL", "触发 B 站风控（412）", COOKIE_HINT + "，或稍后再试")
    if "login" in low or "登录" in msg or "premium" in low or "大会员" in msg:
        return AppError("LOGIN_REQUIRED", "该视频需要登录或会员权限", COOKIE_HINT)
    if "geo" in low or "region" in low or "地区" in msg or "not available in your country" in low:
        return AppError("REGION_BLOCKED", "该视频有地区限制", "可尝试配置 Cookie 或更换网络")
    if "404" in msg or "not found" in low or "不存在" in msg or "-404" in msg:
        return AppError("VIDEO_NOT_FOUND", "视频不存在或已被删除")
    return AppError("DOWNLOAD_FAILED", "获取视频失败：" + _short(msg), "可尝试更新下载组件或配置 Cookie")


def _short(s: str, n: int = 200) -> str:
    s = s.replace("ERROR: ", "").strip()
    return s if len(s) <= n else s[:n] + "…"
