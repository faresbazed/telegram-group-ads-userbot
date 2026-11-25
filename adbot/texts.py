"""Arabic text for the Telegram control bot. English remains the source language."""
import re

AR = {
    # menu and navigation
    "Telegram Group Ads": "إعلانات مجموعات تيليجرام",
    "Campaigns": "الحملات",
    "New campaign": "حملة جديدة",
    "Account status": "حالة الحساب",
    "Help": "مساعدة",
    "Menu": "القائمة",
    "Previous": "السابق",
    "Next": "التالي",
    "New": "جديدة",
    "Cancel": "إلغاء",
    "Campaign": "الحملة",
    "Back to campaigns": "رجوع للحملات",
    # campaign buttons
    "Edit ad": "تعديل الإعلان",
    "Change interval": "تغيير المدة",
    "Add groups": "إضافة جروبات",
    "Remove groups": "حذف جروبات",
    "Disable": "إيقاف",
    "Enable": "تشغيل",
    "Next sends": "مواعيد الإرسال",
    "Send now": "إرسال الآن",
    "Delete": "حذف",
    "Preview": "معاينة",
    "Confirm delete": "تأكيد الحذف",
    "Configured groups": "جروبات الحملة",
    "Joined groups": "جروباتي",
    "Pause all sending": "إيقاف الإرسال كله",
    "Resume all sending": "تشغيل الإرسال كله",
    "Yes, resume sending": "نعم، شغّل الإرسال",
    # prompts
    "No campaigns configured.": "مفيش حملات لسه.",
    "Enter a new campaign name, for example weekend_sale.": "اكتب اسم الحملة الجديدة بحروف إنجليزي وأرقام، مثال: weekend_sale",
    "Enter the ad text to post.": "اكتب نص الإعلان اللي هيتنشر.",
    "Enter an interval, for example 30m, 2h, or 1d.": "اكتب المدة بين كل إرسال، مثال: 30m أو 2h أو 1d.",
    "Use the confirm button or /confirm to delete, or /cancel.": "اضغط زر التأكيد أو اكتب /confirm للحذف، أو /cancel للإلغاء.",
    "Enter the replacement ad text, or /cancel.": "اكتب نص الإعلان الجديد، أو /cancel للإلغاء.",
    "Enter the new interval, for example 30m or 2h, or /cancel.": "اكتب المدة الجديدة، مثال: 30m أو 2h، أو /cancel للإلغاء.",
    "Enter groups to remove, or select a configured group.": "اكتب الجروبات اللي عايز تحذفها، أو اختار من جروبات الحملة.",
    "Enter group IDs, @usernames, or Telegram links (separate multiple groups with spaces).":
        "اكتب أرقام الجروبات أو @يوزر أو روابط تيليجرام (افصل بينهم بمسافة)، أو اختار من جروباتي.",
    "Choose a group to remove.": "اختار الجروب اللي عايز تحذفه.",
    "No joined groups found.": "مفيش جروبات منضم ليها.",
    "Pick the groups this campaign should post in. Tap again to unselect.":
        "اختار الجروبات اللي الحملة هتنشر فيها. اضغط مرة تانية لإلغاء الاختيار.",
    # results
    "Sent to all selected groups.": "اتبعت لكل الجروبات المختارة.",
    "Some or all groups were not sent. Check Status and the runner logs.":
        "في جروبات ماتبعتش. راجع حالة الحساب وسجلات التشغيل.",
    "Campaign settings are busy. Try again.": "إعدادات الحملات مشغولة. جرّب تاني.",
    "Telegram restricted the personal account. Sending is paused; see Status.":
        "تيليجرام قيّد الحساب الشخصي. الإرسال متوقف، راجع حالة الحساب.",
    "Telegram refused access to that group. Other groups are not affected.":
        "تيليجرام رفض الوصول للجروب ده. باقي الجروبات مش متأثرة.",
    "That button has expired. Open /list to start again.": "الزر ده انتهت صلاحيته. افتح /list وابدأ من جديد.",
    "Unknown action. Open /start to see the menu.": "أمر غير معروف. افتح /start لعرض القائمة.",
    "That confirmation has expired. Start again from the campaign menu.": "التأكيد انتهت صلاحيته. ابدأ من قائمة الحملة.",
    "No pending deletion.": "مفيش حذف معلّق.",
    "Campaign changed. Open it again before deleting.": "الحملة اتغيّرت. افتحها تاني قبل الحذف.",
    "Campaign changed. Open it again before editing groups.": "الحملة اتغيّرت. افتحها تاني قبل تعديل الجروبات.",
    "This menu is outdated. Open the current group picker again.": "القائمة دي قديمة. افتح اختيار الجروبات من جديد.",
    "That group selection has expired.": "اختيار الجروب انتهت صلاحيته.",
    "That group is no longer configured for the campaign.": "الجروب ده مش موجود في الحملة.",
    "That group is no longer in your joined list.": "الجروب ده مش في جروباتك دلوقتي.",
    "That group is not configured for the campaign.": "الجروب ده مش مضاف للحملة.",
    "A campaign must have at least one group. Add another group or delete the campaign.":
        "لازم الحملة يكون فيها جروب واحد على الأقل. ضيف جروب تاني أو احذف الحملة.",
    "Enable the campaign before sending.": "شغّل الحملة الأول قبل الإرسال.",
    "Campaign message must not be blank.": "نص الإعلان مينفعش يبقى فاضي.",
    "Enter at least one group ID, @username, or Telegram link.": "اكتب جروب واحد على الأقل: رقم أو @يوزر أو رابط تيليجرام.",
    "Use the confirm button or /confirm to delete.": "اضغط زر التأكيد أو اكتب /confirm للحذف.",
    # status
    "Sending paused: Telegram restricted the personal account.": "الإرسال متوقف: تيليجرام قيّد الحساب الشخصي.",
    "No account-wide sending pause.": "مفيش إيقاف على مستوى الحساب.",
    "Sending is stopped by you. Nothing is sent until you resume it.": "الإرسال متوقف بأمرك. مش هيتبعت أي حاجة لحد ما تشغّله.",
    "Sending is on.": "الإرسال شغّال.",
    "Sending paused. Nothing will be sent until you resume it.": "تم إيقاف الإرسال. مش هيتبعت أي حاجة لحد ما تشغّله.",
    "Resume sending? Enabled campaigns will start posting to their selected groups on schedule.":
        "تشغّل الإرسال؟ الحملات المفعّلة هتبدأ تنشر في جروباتها حسب المواعيد.",
    "Sending resumed.": "تم تشغيل الإرسال.",
    "Sending is already on.": "الإرسال شغّال بالفعل.",
    "Sending is paused. Resume it before sending.": "الإرسال متوقف. شغّله الأول قبل الإرسال.",
    "sending is paused; review the account restriction before resuming":
        "الإرسال متوقف؛ راجع قيد الحساب قبل ما تشغّله.",
    "sending is paused": "الإرسال متوقف",
    "no campaigns": "مفيش حملات",
    "Enabled": "شغّالة",
    "Disabled": "متوقفة",
    "Never sent": "لسه ماتبعتش",
    "Last attempt": "آخر محاولة",
    "Every": "كل",
    "Groups": "الجروبات",
    "Groups count": "عدد الجروبات",
    "Showing the first 12 targets.": "بعرض أول ١٢ جروب بس.",
    "not found among joined groups": "مش موجود في جروباتك",
    "due now": "مستحق دلوقتي",
    "Next sends:": "مواعيد الإرسال:",
    "is disabled.": "متوقفة.",
    "Ad text exactly as it will be posted:": "نص الإعلان بالظبط زي ما هيتنشر:",
    "Nothing was sent.": "مفيش حاجة اتبعتت.",
    "Characters": "عدد الحروف",
    "Help text": "",
}

HELP_AR = (
    "/start — القائمة\n/list — الحملات\n/new — حملة جديدة\n/status — حالة الحساب\n"
    "/pause — إيقاف الإرسال كله فورًا\n/resume — تشغيل الإرسال كله (بيطلب تأكيد)\n"
    "/campaign الاسم — إدارة حملة\n/preview الاسم — معاينة الإعلان بدون إرسال\n"
    "/edit الاسم — تعديل النص\n/interval الاسم — تغيير المدة\n"
    "/addgroup الاسم — إضافة جروبات\n/removegroup الاسم — حذف جروبات\n"
    "/enable الاسم أو /disable الاسم\n/sendnow الاسم — إرسال الآن\n/next الاسم — مواعيد الإرسال\n"
    "/delete الاسم ثم /confirm — حذف\n/cancel — إلغاء الخطوة الحالية"
)

PATTERNS = [
    (r"Telegram requires waiting (\d+) seconds\. Do not press again until then; the bot will not retry by itself\.",
     r"تيليجرام طلب انتظار \1 ثانية. ماتضغطش تاني قبل ما تعدّي، والبوت مش هيعيد المحاولة لوحده."),
    (r"Telegram asked to wait (\d+) more seconds before the group list can load again\.",
     r"تيليجرام طلب انتظار \1 ثانية كمان قبل ما قائمة الجروبات تتحمّل تاني."),
    (r"Enabled: (.*)", r"شغّالة: \1"),
    (r"Disabled: (.*)", r"متوقفة: \1"),
    (r"Save (\d+) selected group\(s\)", r"حفظ \1 جروب مختار"),
    (r"Save (\d+) selected", r"حفظ \1 مختار"),
    (r"Delete (.+)\? This removes the campaign from the schedule\.", r"تحذف الحملة \1؟ هتتشال من الجدول."),
    (r"Campaign already exists: (.*)", r"الحملة موجودة بالفعل: \1"),
    (r"unknown campaign: (.*)", r"حملة غير معروفة: \1"),
    (r"Telegram requires waiting (\d+) seconds\. See Status before retrying\.",
     r"تيليجرام طلب انتظار \1 ثانية. راجع حالة الحساب قبل ما تجرّب تاني."),
    (r"Could not complete that action \((.*)\)\. Try again or check the runner logs\.",
     r"ماقدرتش أنفّذ الأمر (\1). جرّب تاني أو راجع سجلات التشغيل."),
    (r"Telegram flood wait until (.*)\.", r"تيليجرام مانع الإرسال مؤقتًا لحد \1."),
    (r"Telegram flood wait: sending paused until (.*)\.", r"تيليجرام مانع الإرسال مؤقتًا لحد \1."),
    (r"campaign (\S+): message exceeds Telegram's 4096-character limit",
     r"الحملة \1: النص أطول من حد تيليجرام (٤٠٩٦ حرف)"),
    (r"Campaign message exceeds Telegram's 4096-character limit\.", "النص أطول من حد تيليجرام (٤٠٩٦ حرف)."),
    (r"interval must be a positive whole number \(up to nine digits\) with s, m, h or d",
     "المدة لازم تكون رقم صحيح موجب بعده s أو m أو h أو d، مثال: 30m أو 2h."),
    (r"interval must look like 30m, 2h or 1d", "المدة لازم تكون زي 30m أو 2h أو 1d."),
    (r"group must be an ID, @username or t\.me link", "الجروب لازم يكون رقم أو @يوزر أو رابط t.me."),
    (r"campaign name must have 1–64 letters, digits, underscores or hyphens",
     "اسم الحملة من ١ لـ ٦٤ حرف إنجليزي أو رقم أو _ أو -."),
]
_COMPILED = [(re.compile(f"(?s)^{pattern}$"), repl) for pattern, repl in PATTERNS]


def translate(text: str, lang: str) -> str:
    if lang != "ar" or not text:
        return text
    if text in AR and AR[text]:
        return AR[text]
    for pattern, repl in _COMPILED:
        if pattern.match(text):
            return pattern.sub(repl, text)
    return text
