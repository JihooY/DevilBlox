"""Vending UI translations and per-interaction language scopes.

Only UI templates are translated. Product data, credentials, amounts and URLs
are passed through unchanged. Context variables isolate simultaneous buyers.
"""
from contextvars import ContextVar
from functools import wraps
import inspect
from utils.preferences import LANGUAGES

LANGUAGE = ContextVar("vending_language", default="ko")
OWNER = ContextVar("vending_owner", default=None)

# Korean source template: (English, Japanese, Simplified Chinese).
TRANSLATIONS = {
    "충전": ("Top up", "チャージ", "充值"),
    "상품목록": ("Products", "商品一覧", "商品列表"),
    "구매": ("Buy", "購入", "购买"),
    "다운로드": ("Downloads", "ダウンロード", "下载"),
    "충전 신청": ("Top-up request", "チャージ申請", "充值申请"),
    "충전 수락": ("Top-up approved", "チャージ承認", "充值已批准"),
    "충전 거절": ("Top-up rejected", "チャージ拒否", "充值被拒绝"),
    "사유 없음": ("No reason given", "理由なし", "未提供原因"),
    "{0:,}원이 충전되었습니다.\n승인 관리자: {1}": ("KRW {0:,} was added to your balance.\nApproved by: {1}", "{0:,}ウォンがチャージされました。\n承認管理者: {1}", "已充值{0:,}韩元。\n批准管理员：{1}"),
    "사유: {0}\n거절 관리자: {1}": ("Reason: {0}\nRejected by: {1}", "理由: {0}\n拒否管理者: {1}", "原因：{0}\n拒绝管理员：{1}"),
    "상품 구매": ("Purchase product", "商品購入", "购买商品"),
    "상품 목록": ("Product catalog", "商品一覧", "商品列表"),
    "상품": ("Product", "商品", "商品"),
    "카테고리": ("Category", "カテゴリー", "分类"),
    "상품 ID": ("Product ID", "商品ID", "商品ID"),
    "상품 ID: `{0}`": ("Product ID: `{0}`", "商品ID: `{0}`", "商品ID：`{0}`"),
    "가격": ("Price", "価格", "价格"),
    "가격: `{0:,}원`": ("Price: `KRW {0:,}`", "価格: `{0:,}ウォン`", "价格：`{0:,}韩元`"),
    "{0:,}원": ("KRW {0:,}", "{0:,}ウォン", "{0:,}韩元"),
    "{0:,}원 · ID: {1}": ("KRW {0:,} · ID: {1}", "{0:,}ウォン · ID: {1}", "{0:,}韩元 · ID: {1}"),
    "{0:,}원 · ID: {1} · 재고 {2}개": ("KRW {0:,} · ID: {1} · Stock: {2}", "{0:,}ウォン · ID: {1} · 在庫 {2}個", "{0:,}韩元 · ID: {1} · 库存 {2}件"),
    "- `{0}` · {1} · {2:,}원": ("- `{0}` · {1} · KRW {2:,}", "- `{0}` · {1} · {2:,}ウォン", "- `{0}` · {1} · {2:,}韩元"),
    " · 재고 {0}개": (" · Stock: {0}", " · 在庫 {0}個", " · 库存 {0}件"),
    "재고": ("Stock", "在庫", "库存"),
    "재고: `{0}개`": ("Stock: `{0}`", "在庫: `{0}個`", "库存：`{0}件`"),
    "{0}개": ("{0} units", "{0}個", "{0}件"),
    "품절": ("Sold out", "売り切れ", "售罄"),
    " · 품절": (" · Sold out", " · 売り切れ", " · 售罄"),
    " (품절)": (" (Sold out)", " (売り切れ)", "（售罄）"),
    "상품 페이지": ("Product page", "商品ページ", "商品页面"),
    "상품 페이지: {0}": ("Product page: {0}", "商品ページ: {0}", "商品页面：{0}"),
    "`미설정`": ("`Not configured`", "`未設定`", "`未设置`"),
    "셀러": ("Seller", "販売者", "卖家"),
    "상품 정보": ("Product information", "商品情報", "商品信息"),
    "상품 다운로드": ("Product download", "商品ダウンロード", "商品下载"),
    "구매하기": ("Buy now", "購入する", "立即购买"),
    "구매할 상품 ID를 입력하세요.": ("Enter the product ID to purchase.", "購入する商品IDを入力してください。", "请输入要购买的商品ID。"),
    "등록된 상품 설명이 없습니다.": ("No product description available.", "商品説明が登録されていません。", "暂无商品说明。"),
    "이미 구매한 상품입니다. 다운로드 버튼으로 링크를 다시 받을 수 있습니다.": ("You own this product. Use Downloads to retrieve the link again.", "購入済みの商品です。ダウンロードボタンでリンクを再取得できます。", "您已购买此商品，可通过下载按钮重新获取链接。"),
    "상품을 선택하면 상세 정보와 구매 버튼이 표시됩니다.": ("Select a product to see details and purchase options.", "商品を選択すると詳細情報と購入ボタンが表示されます。", "选择商品以查看详情和购买按钮。"),
    "상품을 선택하세요.": ("Select a product.", "商品を選択してください。", "请选择商品。"),
    "상품 선택 ({0}/{1})": ("Select product ({0}/{1})", "商品選択 ({0}/{1})", "选择商品（{0}/{1}）"),
    "카테고리를 선택하세요.": ("Select a category.", "カテゴリーを選択してください。", "请选择分类。"),
    "카테고리 선택 ({0}/{1})": ("Select category ({0}/{1})", "カテゴリー選択 ({0}/{1})", "选择分类（{0}/{1}）"),
    "## {0}\n카테고리를 선택하면 해당 카테고리의 상품이 표시됩니다.": ("## {0}\nSelect a category to browse its products.", "## {0}\nカテゴリーを選択すると商品が表示されます。", "## {0}\n选择分类以查看该分类的商品。"),
    "- 외 {0}개": ("- And {0} more", "- 他 {0}件", "- 另有 {0}件"),
    "등록된 상품이 없습니다.": ("No products registered.", "登録済みの商品はありません。", "暂无商品。"),
    "등록된 카테고리가 없습니다.": ("No categories registered.", "登録済みのカテゴリーはありません。", "暂无分类。"),
    "카테고리 없음": ("No category", "カテゴリーなし", "无分类"),
    "상품 없음": ("No product", "商品なし", "无商品"),
    "선택한 카테고리를 찾을 수 없습니다.": ("The selected category was not found.", "選択したカテゴリーが見つかりません。", "找不到所选分类。"),
    "선택한 상품을 찾을 수 없습니다.": ("The selected product was not found.", "選択した商品が見つかりません。", "找不到所选商品。"),
    "해당 상품 ID를 찾을 수 없습니다.": ("Product ID not found.", "商品IDが見つかりません。", "找不到该商品ID。"),
    "이 카테고리에 상품이 없습니다.": ("This category has no products.", "このカテゴリーに商品はありません。", "此分类暂无商品。"),
    "이 카테고리에 등록된 판매 상품이 없습니다.": ("No products for sale in this category.", "このカテゴリーに販売商品はありません。", "此分类暂无在售商品。"),
    "`/상품카테고리등록`으로 카테고리를 먼저 등록해주세요.": ("Register a category with `/상품카테고리등록` first.", "先に `/상품카테고리등록` でカテゴリーを登録してください。", "请先使用 `/상품카테고리등록` 注册分类。"),
    "처리 실패": ("Request failed", "処理失敗", "处理失败"),
    "서버 안에서만 사용할 수 있습니다.": ("Available only in a server.", "サーバー内でのみ使用できます。", "只能在服务器内使用。"),
    "권한 없음": ("Access denied", "権限なし", "无权限"),
    "본인 세션만 조작할 수 있습니다.": ("You can only use your own session.", "自分のセッションのみ操作できます。", "您只能操作自己的会话。"),
    "입금자명": ("Depositor name", "振込名義", "付款人姓名"),
    "입금자명을 입력하세요.": ("Enter the depositor name.", "振込名義を入力してください。", "请输入付款人姓名。"),
    "입금 금액": ("Deposit amount (KRW)", "振込金額（ウォン）", "付款金额（韩元）"),
    "예: 10000": ("Example: 10000", "例: 10000", "例如：10000"),
    "입금 사진": ("Payment proof", "振込証明画像", "付款凭证"),
    "입금 확인용 이미지 1장을 업로드하세요.": ("Upload one image as payment proof.", "振込確認用の画像を1枚アップロードしてください。", "请上传一张付款凭证图片。"),
    "금액 오류": ("Invalid amount", "金額エラー", "金额错误"),
    "1원 이상의 숫자로 입력해주세요.": ("Enter a number of at least KRW 1.", "1ウォン以上の数値を入力してください。", "请输入至少1韩元的数字。"),
    "입금 사진 없음": ("Missing payment proof", "振込証明なし", "缺少付款凭证"),
    "입금 확인 사진을 1장 업로드해주세요.": ("Upload one payment proof image.", "振込確認画像を1枚アップロードしてください。", "请上传一张付款凭证图片。"),
    "파일 오류": ("Invalid file", "ファイルエラー", "文件错误"),
    "입금 사진은 이미지 파일만 업로드할 수 있습니다.": ("Payment proof must be an image file.", "振込証明には画像ファイルのみ使用できます。", "付款凭证必须是图片文件。"),
    "파일 용량 오류": ("File too large", "ファイル容量エラー", "文件过大"),
    "입금 사진은 8MB 이하로 업로드해주세요.": ("Upload a payment proof image no larger than 8MB.", "振込証明画像は8MB以下にしてください。", "请上传不超过8MB的付款凭证图片。"),
    "파일 처리 실패": ("File processing failed", "ファイル処理失敗", "文件处理失败"),
    "입금 사진을 읽지 못했습니다. 다시 시도해주세요.": ("Could not read the payment proof. Please retry.", "振込証明画像を読み込めませんでした。再試行してください。", "无法读取付款凭证，请重试。"),
    "관리자 채널 미설정": ("Admin channel not configured", "管理者チャンネル未設定", "未设置管理员频道"),
    "`/채널설정`으로 자판기 관리자 채널을 패널 채널과 다른 채널로 설정해주세요.": ("Use `/채널설정` to set a vending admin channel separate from the panel channel.", "`/채널설정` でパネルとは別の管理者チャンネルを設定してください。", "请用 `/채널설정` 将自助售货管理员频道设置为面板以外的频道。"),
    "충전 신청 완료": ("Top-up request submitted", "チャージ申請完了", "充值申请已提交"),
    "관리자가 입금을 확인하면 DM으로 결과를 알려드립니다.": ("You will receive a DM after an administrator checks the payment.", "管理者が入金確認後、DMで結果をお知らせします。", "管理员核实付款后将通过私信通知您。"),
    "잔액 부족": ("Insufficient balance", "残高不足", "余额不足"),
    "현재 잔액은 {0:,}원이고, 상품 가격은 {1:,}원입니다.": ("Your balance is KRW {0:,}; the product costs KRW {1:,}.", "現在の残高は{0:,}ウォン、商品価格は{1:,}ウォンです。", "当前余额为{0:,}韩元，商品价格为{1:,}韩元。"),
    "현재 재고가 없습니다. 입고 후 다시 시도해주세요.": ("Out of stock. Please try again after restocking.", "在庫がありません。入荷後に再試行してください。", "暂无库存，请补货后重试。"),
    "구매 완료": ("Purchase complete", "購入完了", "购买完成"),
    "**{0}** 재고를 DM으로 전송했습니다.": ("Your **{0}** stock item was sent by DM.", "**{0}** の在庫内容をDMで送信しました。", "已通过私信发送 **{0}** 的库存内容。"),
    "상품 `{0}`의 재고 내용입니다.": ("Stock content for product `{0}`.", "商品 `{0}` の在庫内容です。", "商品 `{0}` 的库存内容。"),
    "저장된 내용이 없습니다.": ("No content saved.", "保存された内容はありません。", "暂无已保存内容。"),
    "DM을 보낼 수 없어 아래에 바로 표시합니다. (DM 허용 설정을 확인해주세요)\n{0}": ("DM could not be sent. Your item is shown below; check your DM settings.\n{0}", "DMを送信できないため以下に表示します。DM設定を確認してください。\n{0}", "无法发送私信，内容显示在下方。请检查私信设置。\n{0}"),
    "다운로드 링크": ("Download links", "ダウンロードリンク", "下载链接"),
    "상품 `{0}`의 전달 링크입니다.": ("Delivery link for product `{0}`.", "商品 `{0}` の受け取りリンクです。", "商品 `{0}` 的交付链接。"),
    "선택한 상품의 전달 링크입니다.": ("Delivery links for your selected products.", "選択した商品の受け取りリンクです。", "所选商品的交付链接。"),
    "저장된 링크가 없습니다.": ("No link saved.", "保存されたリンクはありません。", "暂无已保存链接。"),
    "## 다운로드\n링크를 다시 받을 상품을 하나 이상 선택하세요.": ("## Downloads\nSelect one or more products to retrieve their links.", "## ダウンロード\nリンクを再取得する商品を1つ以上選択してください。", "## 下载\n请选择一个或多个商品以重新获取链接。"),
    "다운로드할 상품을 모두 선택하세요.": ("Select all products to download.", "ダウンロードする商品を選択してください。", "请选择所有要下载的商品。"),
    "다운로드 상품 선택 ({0}/{1})": ("Select downloads ({0}/{1})", "ダウンロード商品選択 ({0}/{1})", "选择下载商品（{0}/{1}）"),
    "구매 내역 없음": ("No purchases", "購入履歴なし", "无购买记录"),
    "아직 구매한 상품이 없습니다.": ("You have no purchased products yet.", "まだ購入した商品はありません。", "您尚未购买商品。"),
    "구매한 상품만 다운로드할 수 있습니다.": ("You can only download products you purchased.", "購入済みの商品のみダウンロードできます。", "只能下载已购买的商品。"),
    "쿠폰 / 프로모션": ("Coupons / promotions", "クーポン / プロモーション", "优惠券 / 活动"),
    "쿠폰/프로모션": ("Coupons/promotions", "クーポン/プロモーション", "优惠券/活动"),
    "## 쿠폰 / 프로모션\n보유 쿠폰을 선택하거나 프로모션 코드를 입력하세요.": ("## Coupons / promotions\nSelect a coupon or enter a promotion code.", "## クーポン / プロモーション\nクーポンを選択するかコードを入力してください。", "## 优惠券 / 活动\n请选择优惠券或输入活动代码。"),
    "쿠폰 사용 안 함": ("No coupon", "クーポンを使わない", "不使用优惠券"),
    "보유 쿠폰을 선택하세요": ("Select your coupon", "所有クーポンを選択", "选择您的优惠券"),
    "{0} ({1}장)": ("{0} ({1} available)", "{0} ({1}枚)", "{0}（{1}张）"),
    "{0}% 할인 · {1}": ("{0}% off · {1}", "{0}%割引 · {1}", "优惠{0}% · {1}"),
    "프로모션 코드": ("Promotion code", "プロモーションコード", "活动代码"),
    "프로모션 코드 입력": ("Enter promotion code", "コード入力", "输入活动代码"),
    "사용 불가": ("Unavailable", "使用不可", "不可用"),
    "이 상품은 쿠폰/프로모션을 사용할 수 없습니다.": ("Coupons and promotions cannot be used for this product.", "この商品ではクーポン/プロモーションを使用できません。", "此商品不支持优惠券或活动。"),
    "-# 이 상품은 쿠폰/프로모션을 사용할 수 없습니다.": ("-# Coupons and promotions cannot be used for this product.", "-# この商品ではクーポン/プロモーションを使用できません。", "-# 此商品不支持优惠券或活动。"),
    "쿠폰 오류": ("Coupon error", "クーポンエラー", "优惠券错误"),
    "쿠폰 시스템이 로드되지 않았습니다.": ("The coupon system is not loaded.", "クーポンシステムが読み込まれていません。", "优惠券系统尚未加载。"),
    "해당 프로모션 전용 초대 링크로 가입한 계정만 사용할 수 있습니다.": ("Only accounts that joined through this promotion's invite link are eligible.", "このプロモーション専用の招待リンクで参加したアカウントのみ利用できます。", "仅通过该活动专属邀请链接加入的账号可使用。"),
    "적용 코드: **{0}** (`{1}` · {2})": ("Applied code: **{0}** (`{1}` · {2})", "適用コード: **{0}** (`{1}` · {2})", "已应用代码：**{0}**（`{1}` · {2}）"),
    "할인 금액: **-{0:,}원**": ("Discount: **-KRW {0:,}**", "割引額: **-{0:,}ウォン**", "优惠金额：**-{0:,}韩元**"),
    "적용 후 가격: **{0:,}원**": ("Final price: **KRW {0:,}**", "適用後価格: **{0:,}ウォン**", "优惠后价格：**{0:,}韩元**"),
    "API 지급": ("API delivery", "API付与", "API发放"),
    "API 지급 완료": ("API delivery complete", "API付与完了", "API发放完成"),
    "{0:,} 토큰": ("{0:,} tokens", "{0:,}トークン", "{0:,}个令牌"),
    "{0} 플랜 {1}개월": ("{0} plan for {1} months", "{0}プラン {1}か月", "{0}套餐 {1}个月"),
    "구매 시 지급: `{0}` (반복 구매 가능)": ("Delivery: `{0}` (repeat purchases allowed)", "購入時付与: `{0}`（繰り返し購入可）", "购买后发放：`{0}`（可重复购买）"),
    "{0} 지급을 완료했습니다.\n주문번호: `{1}`": ("Delivered {0}.\nOrder ID: `{1}`", "{0}の付与が完了しました。\n注文番号: `{1}`", "已发放{0}。\n订单编号：`{1}`"),
    "지급 확인 대기": ("Delivery pending", "付与確認待ち", "等待发放确认"),
    "결제한 주문의 지급 완료를 확인하지 못했습니다. 같은 상품의 구매 버튼을 다시 누르면 기존 주문번호로 재시도합니다. 추가 결제되지 않습니다.": ("Delivery is not confirmed yet. Press Buy again for this product to retry the existing order without another charge.", "付与完了を確認できません。同じ商品の購入ボタンで既存注文を再試行できます。追加請求はありません。", "尚未确认发放完成。再次点击该商品的购买按钮将重试原订单，不会重复扣款。"),
    "부스트 주문": ("Boost order", "ブースト注文", "助力订单"),
    "부스트 주문 접수": ("Boost order received", "ブースト注文受付", "助力订单已接收"),
    "부스트 주문: `{0}개월 · {1}개` (반복 구매 가능)": ("Boost order: `{0} months · {1} boosts` (repeat purchases allowed)", "ブースト注文: `{0}か月 · {1}個`（繰り返し購入可）", "助力订单：`{0}个月 · {1}次`（可重复购买）"),
    "{0}개월 · {1}개 · DM에서 초대링크 입력": ("{0} months · {1} boosts · Send invite by DM", "{0}か月 · {1}個 · DMで招待リンク送信", "{0}个月 · {1}次 · 私信发送邀请链接"),
    "구매 후 부스트 봇의 DM에 서버 초대링크를 보내주세요.": ("After purchase, DM the server invite link to the boost bot.", "購入後、ブーストボットのDMにサーバー招待リンクを送信してください。", "购买后，请向助力机器人私信发送服务器邀请链接。"),
    "{0}개월 부스트 {1}개 주문을 접수했습니다.\n부스트 봇의 DM에 서버 초대링크를 보내주세요. DM 수신을 허용해주세요.\n재고가 부족하면 재입고 후 DM에 링크를 다시 보내주세요.\n주문번호: `{2}`\n`/부스트주문조회`로 처리 상태를 확인할 수 있습니다.": ("Received an order for {1} boosts for {0} months.\nDM the server invite link to the boost bot and allow DMs.\nIf stock is insufficient, send the link again after restocking.\nOrder ID: `{2}`\nCheck progress with `/부스트주문조회`.", "{0}か月・{1}個のブースト注文を受け付けました。\nDMを許可し、ブーストボットに招待リンクを送信してください。\n在庫不足の場合は入荷後に再送信してください。\n注文番号: `{2}`\n`/부스트주문조회` で状況を確認できます。", "已接收{0}个月、{1}次助力的订单。\n请允许私信并向助力机器人发送服务器邀请链接。\n如库存不足，请补货后重新发送链接。\n订单编号：`{2}`\n可通过 `/부스트주문조회` 查询进度。"),
    "일반 쿠폰": ("Coupon", "通常クーポン", "普通优惠券"),
    "{0}님이 {1}를 사용했습니다.": ("{0} used {1}.", "{0}が{1}を使用しました。", "{0}使用了{1}。"),
}


def tr(template: str, language: str | None = None) -> str:
    language = language or LANGUAGE.get()
    if language not in LANGUAGES or language == "ko" or template not in TRANSLATIONS:
        return template
    return TRANSLATIONS[template][{"en": 0, "ja": 1, "zh": 2}[language]]


def localized_component(cls):
    """Capture language and owner when built; restore them for every callback."""
    original_init = cls.__init__

    @wraps(original_init)
    def initialize(self, *args, **kwargs):
        self.language = kwargs.pop("language", LANGUAGE.get())
        if self.language not in LANGUAGES:
            self.language = "ko"
        self.user_id = kwargs.pop("user_id", OWNER.get())
        token = LANGUAGE.set(self.language)
        owner_token = OWNER.set(self.user_id)
        try:
            original_init(self, *args, **kwargs)
            # Modal titles are declared at import time; inputs render in __init__.
            if hasattr(self, "title") and hasattr(self, "on_submit"):
                self.title = tr(self.title)
        finally:
            OWNER.reset(owner_token)
            LANGUAGE.reset(token)

    cls.__init__ = initialize
    for name, method in list(vars(cls).items()):
        if not inspect.iscoroutinefunction(method):
            continue

        def scoped(callback):
            @wraps(callback)
            async def invoke(self, interaction, *args, **kwargs):
                token = LANGUAGE.set(self.language)
                owner_token = OWNER.set(self.user_id)
                try:
                    if self.user_id is not None and interaction.user.id != self.user_id:
                        await interaction.response.send_message(tr("본인 세션만 조작할 수 있습니다."), ephemeral=True)
                        return
                    return await callback(self, interaction, *args, **kwargs)
                finally:
                    OWNER.reset(owner_token)
                    LANGUAGE.reset(token)
            return invoke

        setattr(cls, name, scoped(method))
    return cls
