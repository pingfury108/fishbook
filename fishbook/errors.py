"""统一错误类型与退出码。"""

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_ENDPOINT = 2
EXIT_AUTH = 3
EXIT_RISK = 4
EXIT_STRUCTURE = 5
EXIT_ABORT = 6


class FishbookError(Exception):
    """所有可预期错误的基类：带机器可读 code 和退出码。"""

    exit_code = EXIT_USAGE
    code = "ERROR"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class UsageError(FishbookError):
    """参数不合法。"""

    exit_code = EXIT_USAGE
    code = "USAGE"


class EndpointError(FishbookError):
    """连不上 CDP、找不到匹配 target、页面未就绪。"""

    exit_code = EXIT_ENDPOINT
    code = "ENDPOINT"


class CDPError(FishbookError):
    """CDP 协议层错误。"""

    exit_code = EXIT_ENDPOINT
    code = "CDP"


class CommandTimeout(CDPError):
    """单条命令超时。"""

    code = "CDP_TIMEOUT"


class AuthError(FishbookError):
    """登录态失效，需要人工重新登录。"""

    exit_code = EXIT_AUTH
    code = "AUTH_REQUIRED"


class RiskError(FishbookError):
    """触发风控，需人工处理。"""

    exit_code = EXIT_RISK
    code = "RISK_CONTROL"


class BusinessError(FishbookError):
    """MTOP 返回的业务错误。"""

    exit_code = EXIT_USAGE
    code = "MTOP_ERROR"


class StructureError(FishbookError):
    """页面结构不符：选择器失效、输入框不唯一等。"""

    exit_code = EXIT_STRUCTURE
    code = "STRUCTURE"


class NotFoundError(FishbookError):
    """目标内容不存在（按方案映射到退出码 3）。"""

    exit_code = EXIT_AUTH
    code = "NOT_FOUND"


class AbortError(FishbookError):
    """连续失败熔断。"""

    exit_code = EXIT_ABORT
    code = "ABORTED"
