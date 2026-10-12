"""Personal profile copy (Korean, English, Japanese, Simplified Chinese)."""
from utils.preferences import default_language

TEXT = {
    "description": ("기본 언어와 받고 싶은 알림을 설정하세요.", "Choose your default language and notifications.", "基本言語と受け取りたい通知を設定してください。", "设置默认语言和希望接收的通知。"),
    "welcome": ("인증이 완료되었습니다! 아래에서 기본 언어와 알림을 설정하세요.", "Verification complete! Choose your default language and notifications below.", "認証が完了しました！以下で基本言語と通知を設定してください。", "验证完成！请在下方设置默认语言和通知。"),
    "language": ("기본 언어", "Default language", "基本言語", "默认语言"),
    "hint": ("변경 사항은 즉시 저장됩니다. `/profile`에서 언제든 다시 설정할 수 있습니다.", "Changes are saved immediately. Use `/profile` to change them anytime.", "変更はすぐに保存されます。いつでも `/profile` で変更できます。", "更改会立即保存。随时使用 `/profile` 重新设置。"),
    "notifications": ("알림 설정", "Notifications", "通知設定", "通知设置"),
    "alarm_announcement": ("공지 알림", "Announcements", "お知らせ通知", "公告通知"),
    "alarm_seller": ("티켓 상태 알림", "Ticket status", "チケット状態通知", "工单状态通知"),
    "alarm_stock": ("입고 알림", "Restock alerts", "入荷通知", "补货通知"),
    "on": ("켜짐", "On", "オン", "开启"),
    "off": ("꺼짐", "Off", "オフ", "关闭"),
    "missing": ("역할 미설정", "Role not configured", "ロール未設定", "未配置身份组"),
    "missing_error": ("알림 역할이 설정되지 않았습니다. 관리자가 `/역할설정`으로 설정해야 합니다.", "This notification role is not configured. Ask an administrator to use `/역할설정`.", "通知ロールが未設定です。管理者が `/역할설정` で設定してください。", "未配置此通知身份组，请管理员使用 `/역할설정` 设置。"),
    "permission_error": ("봇의 역할 관리 권한과 역할 순서를 확인해주세요. 알림 설정이 변경되지 않았습니다.", "Check the bot's Manage Roles permission and role hierarchy. The notification setting was not changed.", "ボットのロール管理権限と順位を確認してください。通知設定は変更されていません。", "请检查机器人的管理身份组权限和身份组顺序。通知设置未更改。"),
    "retry": ("설정을 변경하지 못했습니다. 잠시 후 `/profile`에서 다시 시도해주세요.", "Could not update your settings. Try `/profile` again shortly.", "設定を更新できませんでした。しばらくしてから `/profile` で再試行してください。", "无法更新设置，请稍后使用 `/profile` 重试。"),
    "owner": ("본인의 프로필만 설정할 수 있습니다.", "You can only change your own profile.", "自分のプロフィールのみ設定できます。", "您只能设置自己的个人资料。"),
    "guild": ("서버 안에서만 사용할 수 있습니다.", "This is only available in a server.", "サーバー内でのみ使用できます。", "只能在服务器内使用。"),
}


def profile_text(key: str, language: str = "ko") -> str:
    language = default_language({"default_language": language})
    return TEXT[key][{"ko": 0, "en": 1, "ja": 2, "zh": 3}[language]]
