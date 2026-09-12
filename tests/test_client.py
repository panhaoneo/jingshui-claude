"""守住 API 调用纪律：Key 只走 Header；错误码分类重试。"""
import pytest

from jingshui import client as C

KEY = "sk-test-not-a-real-key"


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(C.time, "sleep", lambda s: None)


def make(responses):
    s = FakeSession(responses)
    return C.HiThinkClient(api_key=KEY, session=s, min_interval=0), s


def test_key_in_header_never_in_url():
    cl, s = make([FakeResp(body={"code": 0, "data": {"ok": 1}})])
    assert cl.get("/api/meta/tickers/search", {"q": "600519"}) == {"ok": 1}
    url, params = s.calls[0]
    assert KEY not in url and KEY not in str(params)
    assert s.headers["X-api-key"] == KEY
    assert KEY not in repr(cl)


def test_param_error_not_retried():
    cl, s = make([FakeResp(body={"code": 1002, "message": "bad", "data": None})])
    with pytest.raises(C.ApiError) as e:
        cl.get("/x")
    assert e.value.code == 1002 and len(s.calls) == 1
    assert KEY not in str(e.value)


def test_not_ready_not_retried():
    cl, s = make([FakeResp(body={"code": 3002, "message": "not ready", "request_id": "r1", "data": None})])
    with pytest.raises(C.DataNotReady):
        cl.get("/x")
    assert len(s.calls) == 1


def test_rate_limit_retried_then_succeeds():
    cl, s = make([FakeResp(body={"code": 4001, "data": None}), FakeResp(status=502, body={}),
                  FakeResp(body={"code": 0, "data": 7})])
    assert cl.get("/x") == 7 and len(s.calls) == 3


def test_rate_limit_gives_up_after_3_retries():
    cl, s = make([FakeResp(body={"code": 5001, "data": None})] * 4)
    with pytest.raises(C.ApiError):
        cl.get("/x")
    assert len(s.calls) == 4


def test_auth_error():
    cl, _ = make([FakeResp(body={"code": 2003, "message": "invalid", "data": None})])
    with pytest.raises(C.AuthError):
        cl.get("/x")
