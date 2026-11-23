"""RunPod stock snapshots shared by the desktop and reservation worker."""
from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_CEILING
from email.utils import parsedate_to_datetime
import fcntl
import json
import time
from urllib.error import HTTPError
import urllib.request

import gpu

ROOT = gpu.PROFILE
DISK = Decimal("0.007")
NORMAL_INTERVAL = 60
UNCHANGED_INTERVAL = 120
MANUAL_INTERVAL = 15
STALE_AFTER = UNCHANGED_INTERVAL + 20

QUERY = """query {
  gpuTypes {
    id displayName memoryInGb communityPrice securePrice
    communityCloud secureCloud
    community: lowestPrice(input:{gpuCount:1, secureCloud:false, supportPublicIp:true, minCudaVersion:"12.8", minDisk:50}) {
      stockStatus uninterruptablePrice availableGpuCounts
    }
    secure: lowestPrice(input:{gpuCount:1, secureCloud:true, minCudaVersion:"12.8", minDisk:50}) {
      stockStatus uninterruptablePrice availableGpuCounts
    }
  }
}"""


@dataclass(frozen=True)
class Offer:
    model: str
    name: str
    memory: int
    cloud: str
    available: bool | None
    price: Decimal

    @property
    def hourly(self):
        return (self.price + DISK).quantize(Decimal(".01"), rounding=ROUND_CEILING)


def request_offers():
    req = urllib.request.Request("https://api.runpod.io/graphql",
                                 data=json.dumps({"query": QUERY}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as response:
        body = json.load(response, parse_float=Decimal)
    if body.get("errors") or not body.get("data", {}).get("gpuTypes"):
        raise RuntimeError("在庫データを取得できませんでした。")
    return parse_offers(body["data"]["gpuTypes"])


def parse_offers(models):
    offers = []
    for model in models:
        if not model["id"].startswith("NVIDIA"):
            continue
        for tier in ("community", "secure"):
            base = model.get(tier + "Price")
            if not base or not model.get(tier + "Cloud"):
                continue
            data = model.get(tier)
            stock = None
            rate = None
            if isinstance(data, dict):
                rate = data.get("uninterruptablePrice")
                status = str(data.get("stockStatus") or "").lower()
                if rate is None and status in ("", "none", "unavailable", "out of stock"):
                    stock = False
                elif rate is not None and status in ("high", "medium", "low"):
                    counts = data.get("availableGpuCounts")
                    stock = counts is None or 1 in counts
            price = Decimal(str(rate if rate is not None else base))
            if price.is_finite() and price > 0:
                offers.append(Offer(model["id"], model["displayName"], int(model["memoryInGb"]),
                                    tier.upper(), stock, price))
    return sorted(offers, key=lambda offer: (offer.model, offer.cloud))


@dataclass
class Snapshot:
    offers: list = field(default_factory=list)
    checked: float = 0.0
    due: float = 0.0
    failures: int = 0
    error: str = ""

    def is_fresh(self, now):
        return bool(self.checked and not self.error and 0 <= now - self.checked <= STALE_AFTER)


def read_snapshot():
    try:
        data = json.loads((ROOT / "inventory.json").read_text())
        data["offers"] = [Offer(**dict(row, price=Decimal(row["price"]))) for row in data["offers"]]
        return Snapshot(**data)
    except (FileNotFoundError, ValueError, TypeError, KeyError, ArithmeticError):
        return Snapshot()


def retry_seconds(headers, now):
    value = headers.get("Retry-After", "") if headers else ""
    if value.strip().isdigit():
        return int(value.strip())
    try:
        return max(0, parsedate_to_datetime(value).timestamp() - now)
    except (TypeError, ValueError, OverflowError):
        return 0


def snapshot(*, manual=False, waiting=False):
    ROOT.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (ROOT / "inventory.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = read_snapshot()
        now = time.time()
        due = previous.due
        if not previous.error:
            if manual:
                due = min(due, previous.checked + MANUAL_INTERVAL)
            elif waiting:
                due = min(due, previous.checked + NORMAL_INTERVAL)
        if now < due and now >= previous.checked:
            return previous

        try:
            offers = request_offers()
            if not offers:
                raise ValueError("Empty RunPod inventory")
        except Exception as exc:
            now = time.time()
            failures = previous.failures + 1
            delay = min(900, NORMAL_INTERVAL * 2 ** min(failures - 1, 4))
            reason = "通信エラー"
            if isinstance(exc, HTTPError):
                delay = max(delay, retry_seconds(exc.headers, now))
                if exc.code == 429:
                    reason = "アクセス制限"
            current = Snapshot(previous.offers, previous.checked, now + delay, failures, reason)
        else:
            now = time.time()
            unchanged = offers == previous.offers and not previous.error
            interval = UNCHANGED_INTERVAL if unchanged and not waiting else NORMAL_INTERVAL
            current = Snapshot(offers, now, now + interval)
        gpu.atomic_write(ROOT / "inventory.json", json.dumps(asdict(current), default=str))
        return current


def catalog(model=None, cloud=None, limit=None):
    view = snapshot(waiting=model is not None)
    if model is not None and view.is_fresh(time.time()) and time.time() - view.checked >= MANUAL_INTERVAL:
        # Share the recent quote, but recheck an older actionable price at order time.
        if any(o.model == model and o.cloud == cloud and o.available and o.hourly <= limit
               for o in view.offers):
            view = snapshot(manual=True, waiting=True)
    if not view.is_fresh(time.time()):
        raise RuntimeError(f"{view.error or '在庫未確認'}・再確認まで待機します。")
    return view.offers
