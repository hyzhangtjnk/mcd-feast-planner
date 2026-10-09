#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcd_client.py —— 麦当劳 MCP 的最小 HTTP 客户端（零第三方依赖）

设计原则：
  1. 零依赖：仅用 Python 标准库，WorkBuddy 沙箱可直接运行
  2. 可注入：所有工具调用集中在此层，便于 mock 与离线测试
  3. 真实参数：字段名严格对齐 MCP Server 实际 schema（踩过的坑见注释）

接入信息（来自 https://github.com/M-China/mcd-mcp-server）：
  - 地址     https://mcp.mcd.cn
  - 协议     Streamable HTTP，JSON-RPC 2.0
  - 鉴权     Header: Authorization: Bearer <MCP_TOKEN>
  - 限流     每 Token 600 次/分钟，超限返回 429
"""

import json
import os
import ssl
import time
import urllib.error
import urllib.request

MCP_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2024-11-05"

# ---------------------------------------------------------------------------
# 业务常量：beType / orderType 的取值语义
# ---------------------------------------------------------------------------
BE_TYPE = {
    1: "到店取餐",
    2: "麦乐送到家",
    5: "得来速(DT)",
    6: "企业团餐",
}
ORDER_TYPE = {
    1: "到店（含到店自取+得来速）",
    2: "外送（含麦乐送+企业团餐）",
}


class McdMcpError(RuntimeError):
    """MCP 调用失败（HTTP 层或业务层 code != 200）。"""

    def __init__(self, message, code=None, payload=None):
        super().__init__(message)
        self.code = code
        self.payload = payload


class McdMcpClient:
    """麦当劳 MCP Server 客户端。

    用法：
        cli = McdMcpClient()                       # 从环境变量 MCD_MCP_TOKEN 读 token
        cli = McdMcpClient(token="xxx")            # 显式传入
        stores = cli.query_nearby_stores(city="<城市>", keyword="<地标名>")
    """

    def __init__(self, token=None, url=MCP_URL, timeout=60, verbose=False,
                 verify_tls=True):
        self.token = token or os.environ.get("MCD_MCP_TOKEN", "")
        if not self.token:
            raise McdMcpError(
                "缺少 MCP Token：请设置环境变量 MCD_MCP_TOKEN，"
                "或使用 McdMcpClient(token='...')"
            )
        self.url = url
        self.timeout = timeout
        self.verbose = verbose

        # TLS 校验默认**始终开启**（安全默认）。
        # ⚠️ Codex 复审2 中等#1：旧参数名 `insecure_tls` 语义完全反转 —— 传 False
        #    反而会关闭 hostname 校验并置 CERT_NONE，任何按参数名理解的人都会中招。
        #    现改名为 `verify_tls`，默认 True；仅显式传 verify_tls=False 才降级，
        #    并明确标注「仅供本地自建测试端点使用，勿用于生产」。
        if verify_tls:
            ctx = ssl.create_default_context()
            # Windows 上若证书吊销列表不可达，schannel 会抛 CRYPT_E_REVOCATION_OFFLINE。
            # 官方 MCP 域名使用受信任证书，此处仅关闭吊销检查，仍保留证书链校验。
            ctx.verify_flags &= ~ssl.VERIFY_CRL_CHECK_LEAF
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._ctx = ctx

        self._session_id = None
        self._rpc_id = 0
        self._initialized = False
        self._call_count = 0

    # ------------------------------------------------------------------
    # 底层 JSON-RPC
    # ------------------------------------------------------------------
    def _post(self, payload):
        """发送一次 JSON-RPC 请求，返回解析后的结果。"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.token,
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id

        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        last_err = None
        for attempt in range(3):
            try:
                resp = urllib.request.urlopen(
                    req, context=self._ctx, timeout=self.timeout
                )
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                if e.code == 429:
                    # 限流：退避后重试
                    last_err = McdMcpError(
                        "触发限流(429)，请降低调用频率", code=429, payload=body
                    )
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise McdMcpError(
                    "HTTP %d: %s" % (e.code, body[:300]), code=e.code, payload=body
                )
            except (urllib.error.URLError, ssl.SSLError, OSError) as e:
                last_err = McdMcpError("网络错误: %s" % e)
                time.sleep(1.0 * (attempt + 1))
                continue

        else:
            raise last_err if last_err else McdMcpError("未知网络错误")

        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid

        raw = resp.read().decode("utf-8", errors="replace").strip()
        if not raw:
            return None

        # Streamable HTTP 可能以 SSE 形式返回：data: {...}
        if raw.startswith("event:") or raw.startswith("data:"):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    break

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise McdMcpError("响应不是合法 JSON: %s" % raw[:300])

    def _notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload)

    def _rpc(self, method, params=None):
        self._rpc_id += 1
        payload = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method}
        if params is not None:
            payload["params"] = params
        result = self._post(payload)
        if result is None:
            return None
        if "error" in result:
            err = result["error"]
            raise McdMcpError("RPC 错误: %s" % err, payload=err)
        return result.get("result")

    def connect(self):
        """完成一次 initialize + initialized 握手（幂等）。"""
        if self._initialized:
            return self
        init = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcd-feast-planner", "version": "1.0.0"},
            },
        )
        self._notify("notifications/initialized")
        self._initialized = True
        self.server_info = (init or {}).get("serverInfo", {})
        return self

    # ------------------------------------------------------------------
    # 工具层
    # ------------------------------------------------------------------
    def list_tools(self):
        """列出服务端可用工具（用于自检与文档核对）。"""
        self.connect()
        res = self._rpc("tools/list") or {}
        return res.get("tools", [])

    def call_tool(self, name, arguments=None):
        """调用任意工具，返回工具返回的 structuredContent（业务数据）。

        MCP 工具返回值形如：
            {"content": [{"type": "text", "text": "..."}],
             "structuredContent": {...实际业务数据...},
             "isError": false}
        本方法优先返回 structuredContent；若不存在则解析 content 中的 JSON。
        """
        self.connect()
        self._call_count += 1
        if self.verbose:
            print("  [mcp] -> %s %s" % (name, json.dumps(arguments or {}, ensure_ascii=False)))
        res = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if res is None:
            return None

        if res.get("isError"):
            raise McdMcpError("工具 %s 返回错误" % name, payload=res)

        sc = res.get("structuredContent")
        if sc is not None:
            return self._unwrap(sc)

        # 退化路径：从 content[].text 中抽取 JSON
        for item in res.get("content", []):
            text = item.get("text", "")
            idx = text.find("{")
            if idx >= 0:
                try:
                    return self._unwrap(json.loads(text[idx:]))
                except json.JSONDecodeError:
                    pass

        # ⚠️ Codex 复审2 轻微#2：当 MCP 未提供 structuredContent、且 text 不是
        #    JSON 时（例如营养表样例以 "[160]{字段...}" 开头），旧实现直接
        #    `return res`（整个信封 dict）。调用方 parse_nutrition() 只接受字符串，
        #    于是营养表**静默变成空表** —— 用户以为"没有营养数据"，实则被吞掉。
        #    这里显式把单条文本原样返回，让下游能真正解析到内容。
        return self._extract_text_or_res(res)

    @staticmethod
    def _extract_text_or_res(res):
        """退化路径：无 structuredContent 时，取文本内容；否则返回原响应。

        ⚠️ Codex 复审3 中等#6：原实现只处理「恰好一个文本块」，MCP 若以**多个**
        纯文本块返回（营养表较长时常见）就会落到 `return res`，下游
        `parse_nutrition()` 收到 dict → 静默变成空表。现改为拼接**全部**
        `type == "text"` 的内容块，保证内容不丢。
        """
        content = res.get("content") or []
        if not isinstance(content, list):
            return res
        texts = [it.get("text") for it in content
                 if isinstance(it, dict) and it.get("type") == "text"
                 and isinstance(it.get("text"), str)]
        texts = [t for t in texts if t and t.strip()]
        if not texts:
            return res
        if len(texts) == 1:
            return texts[0]
        return "\n".join(texts)

    @staticmethod
    def _unwrap(payload):
        """统一处理 {success, code, data} 信封，业务失败时抛错。"""
        if isinstance(payload, dict) and "success" in payload:
            if not payload.get("success"):
                raise McdMcpError(
                    payload.get("message", "业务调用失败"),
                    code=payload.get("code"),
                    payload=payload,
                )
            return payload.get("data")
        return payload

    # ---------------- 具体工具封装（字段名严格对齐实际 schema）----------

    def now_time_info(self):
        return self.call_tool("now-time-info", {})

    def query_nearby_stores(self, city, keyword, be_type=1, search_type=2):
        """查询附近门店。searchType=2 按位置搜索时 city/keyword 必填。"""
        return self.call_tool(
            "query-nearby-stores",
            {"beType": be_type, "searchType": search_type,
             "city": city, "keyword": keyword},
        )

    def query_meals(self, store_code, be_type=1, order_type=1, be_code=None):
        """查询门店可售餐品列表。

        ⚠️ 注意：返回的 meals[] 只有 code 与 tags，**不含名称与价格**。
        名称/价格必须通过 query_meal_detail 或 calculate_price 获取。
        """
        args = {"storeCode": store_code, "beType": be_type, "orderType": order_type}
        _attach_becode(args, be_type, order_type, be_code)
        return self.call_tool("query-meals", args)

    def query_meal_detail(self, store_code, code, be_type=1, order_type=1, be_code=None):
        """查询餐品详情（名称、图片、套餐组成、可选替换项、特制选项）。

        ⚠️ 参数名是 code，不是 mealCode。
        """
        args = {"storeCode": store_code, "beType": be_type,
                "orderType": order_type, "code": code}
        _attach_becode(args, be_type, order_type, be_code)
        return self.call_tool("query-meal-detail", args)

    def calculate_price(self, store_code, items, be_type=1, order_type=1,
                        be_code=None):
        """计算价格（金额单位：分）。

        ⚠️ items[] 的商品编码字段名是 productCode，不是 code。
           传错字段名不报错，但会静默返回全 0 金额 —— 极易踩坑。
        ⚠️ AGY 复审2 轻微#3：原先还声明了 coupon_code / coupon_id 形参，
           但它们从未挂进请求体（属未生效的遗留参数），容易误导调用方以为
           传了券就会生效。本工具定位「只算不买、不做券核销」，故移除该形参。
           券相关能力请使用 query_store_coupons / available_coupons 查询。
        """
        args = {"storeCode": store_code, "beType": be_type,
                "orderType": order_type, "items": items}
        _attach_becode(args, be_type, order_type, be_code)
        return self.call_tool("calculate-price", args)

    def query_store_coupons(self, store_code, be_type=1, order_type=1, be_code=None):
        """查询【指定门店+订单类型】下可用的券（下单规则已校验）。"""
        args = {"storeCode": store_code, "beType": be_type, "orderType": order_type}
        _attach_becode(args, be_type, order_type, be_code)
        return self.call_tool("query-store-coupons", args)

    def available_coupons(self):
        """用户当前**可领取**的麦麦省券列表（尚未领取）。"""
        return self.call_tool("available-coupons", {})

    def query_my_coupons(self, page="1", page_size="200"):
        """用户卡包中**已拥有**的券资产。

        ⚠️ 官方明确：本接口**不做门店/渠道/配送方式的下单规则校验**，
           不承诺该券可用于当前订单。要判断「当前门店/当前订单可用券」，
           必须改用 query_store_coupons()。
        """
        return self.call_tool("query-my-coupons",
                              {"page": page, "pageSize": page_size})

    def auto_bind_coupons(self):
        """一键领取麦麦省全部当前可用券（**写操作**）。

        ⚠️ 写操作：会真实改变用户账户的券资产。默认不由主流程调用，
           仅在用户显式确认「帮我领券」时执行。
        """
        return self.call_tool("auto-bind-coupons", {})

    # ---------------- 下单与订单（写操作，需用户显式确认）----------------

    def create_order(self, store_code, items, be_type=1, order_type=1,
                     be_code=None, take_way_code=None, address_id=None,
                     reservation_date=None, gm_service_code=None):
        """创建麦当劳订单（**写操作**）。

        参数规则（严格对齐官方 schema）：
          · orderType=1（到店）：必传 takeWayCode —— 取值来自
            calculate_price() 返回的 data.takeWayList[].code
          · orderType=2（外送）：必传 addressId
          · beType=5（得来速）/ 6（团餐）/ orderType=2 → 必传 beCode
          · beType=6（团餐）→ 必传 gmServiceCode（来自 query-meal-assistance）
          · items[] 中可带 couponId / couponCode 使用优惠券

        ⚠️ 本方法**不会**自动执行。CLI 侧默认仅打印将要提交的报文（dry-run），
           必须用户显式加 --confirm 才会真实下单。
        """
        args = {"storeCode": store_code, "beType": be_type,
                "orderType": order_type, "items": items}
        _attach_becode(args, be_type, order_type, be_code)
        if order_type == 1:
            if not take_way_code:
                raise McdMcpError(
                    "orderType=1（到店）必须提供 takeWayCode，"
                    "它来自 calculate-price 返回的 data.takeWayList[].code"
                )
            args["takeWayCode"] = take_way_code
        else:
            if not address_id:
                raise McdMcpError("orderType=2（外送）必须提供 addressId")
            args["addressId"] = address_id
        if be_type == 6:
            if not gm_service_code:
                raise McdMcpError("beType=6（团餐）必须提供 gmServiceCode")
            args["gmServiceCode"] = gm_service_code
        if reservation_date:
            args["reservationDate"] = reservation_date
        return self.call_tool("create-order", args)

    def query_order(self, order_id):
        """查询订单详情与**支付状态**（用户说"我已支付/支付失败"时用它复核）。"""
        return self.call_tool("query-order", {"orderId": order_id})

    def order_list(self):
        """查询本人麦当劳历史订单（不含商城订单）。"""
        return self.call_tool("order-list", {})

    def list_nutrition_foods(self):
        """获取餐品营养数据（160 条），返回原始字符串（TSV 形式）。"""
        return self.call_tool("list-nutrition-foods", {})

    # ------------------------------------------------------------------
    # 关于「支付」的诚实说明（务必保留）
    #
    # 麦当劳 MCP 服务端**没有独立的支付工具**（已实测 tools/list 全量 35 个工具）。
    # 真实链路是：
    #   1) create-order 生成订单 → 返回订单号，此时订单处于**待支付**状态；
    #   2) 用户在**麦当劳官方渠道**（麦当劳 App / 小程序 / 门店）完成支付；
    #   3) 用 query-order(orderId) 复核支付状态（已支付 / 支付失败 / 配送进度）。
    #
    # 因此本项目的能力边界是「**下单到待支付为止**」：
    # 能创建订单、能核价、能选券、能查支付结果，但**不代收款项、不代付款**。
    # 这是接口能力的事实边界，不是实现缺失 —— 任何声称「代付」的表述都是误导。
    # ------------------------------------------------------------------

    @property
    def call_count(self):
        """已发起的工具调用次数（用于展示对限流的友好程度）。"""
        return self._call_count


def _attach_becode(args, be_type, order_type, be_code):
    """beCode 传递规则（来自官方 schema）：
       到店自取(beType=1) → 不传，传了会报错
       得来速(beType=5) / 团餐(beType=6) / 外送(orderType=2) → 必传
    """
    # ⚠️ AGY 审查：原实现漏了 beType=6（团餐），团餐场景不传 beCode 会失败。
    need = be_type in (5, 6) or order_type == 2
    if need and be_code:
        args["beCode"] = be_code
    elif need and not be_code:
        raise McdMcpError(
            "beType=%s / orderType=%s 场景必须提供 beCode" % (be_type, order_type)
        )
