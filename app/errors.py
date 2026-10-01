"""领域错误码。"""


class DomainError(Exception):
    """所有业务规则违例的基类。"""

    code = "domain_error"

    def __init__(self, message: str = "", code: str | None = None):
        super().__init__(message or self.__class__.__doc__)
        if code:
            self.code = code


class RevisionStale(DomainError):
    """版次已被新版本取代，旧首箱结论失效。"""

    code = "revision_stale"


class MissingItem(DomainError):
    """采集缺项（照片缺面等），阻断放行。"""

    code = "missing_item"


class BlockingFinding(DomainError):
    """存在不可放行的阻断项。"""

    code = "blocking_finding"


class SelfApproval(DomainError):
    """操作员不能批准自己的首箱。"""

    code = "self_approval"


class WrongRole(DomainError):
    """只有质检员可以放行/判返工。"""

    code = "wrong_role"


class AlreadyApproved(DomainError):
    """该首箱已有放行结论（并发时仅一人成功）。"""

    code = "already_approved"


class SampleLocked(DomainError):
    """旧版次留样已归档只读，禁止修改。"""

    code = "sample_locked"


class DuplicateActiveSample(DomainError):
    """同一订单同一版次只能有一份生效留样。"""

    code = "duplicate_active_sample"


class IllegalTransition(DomainError):
    """状态机不允许该流转。"""

    code = "illegal_transition"
