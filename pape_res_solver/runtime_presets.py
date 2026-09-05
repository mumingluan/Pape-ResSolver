from __future__ import annotations

# Keep this list synchronized with the configuration tables decoded by
# Pape-BOOI/internal/gamedata. It is intentionally explicit: a new server
# dependency must be reviewed before the runtime database format changes.
#
# 2026-09-05: Added 26 names recovered from the v1.7.1550 client. The list was
# derived by:
#   1. Reverse-scanning 30,737 .luac files in ResGot_1.7.1550 for
#      LuaCfgMgr.Get/GetAll/GetListByCondition/GetByCondition("X") calls.
#   2. Computing `referenced - existing_tables - BOOI_RUNTIME_TABLES` against
#      booi-resources_1.7.1550.sqlite.
#   3. Verifying each name is truly absent from booi (sql count -> 0).
# Names ending in "." (PopRunMap., PopRunMap.Pre., PopRunNoteGroup.) and the
# "HomeTemplate.HomeTemplate_" pattern are kept because they match what the
# IL2CPP static analysis found in real Lua callers; they will only register
# server-side sub-locale lookups.
BOOI_RUNTIME_TABLES = frozenset(
    {
        "ASMRInfo", "AchievementType", "ActivityReward", "ActivityTechTree",
        "CircleChess",
        "Dialogue", "DialogueVoiceRecognition",
        "EasterEgg",
        "GachaRuleText", "GMCompose_DEBUG", "GMList_DEBUG", "GM_DEBUG",
        "GoogleTaskString",
        "HomeTemplate.HomeTemplate_",
        "PayInfo", "PhoneMoment", "PhotoCode",
        "PopRunMap.", "PopRunMap.Pre.", "PopRunNoteGroup.",
        "RadioInfo", "RoleDelectionTxt", "Rule",
        "SCoreStoryInfo", "StorySeriesContent", "SystemURLList",
        "ActivityCenter", "ActivityTotalLogin", "AnecdoteInfo", "AnecdoteSection",
        "CardAwake", "CardBaseInfo", "CardLevelExp", "CardRare", "CardReward", "CardSet",
        "CardStar", "CfgConst", "ChapterInfo", "CollectionInfo", "CommonCondition",
        "CommonStageEntry", "DiamondBuyStamina", "FashionData", "FashionDefault",
        "FashionMode", "FashionModeDefault", "FormationSuit", "Frame", "GachaAll",
        "GachaCountReward", "GachaDrop", "GachaGroup", "GachaRule", "GalleryDollCollection",
        "GemCoreBaseInfo", "GemCoreLevel", "GemRare", "GuideStep", "HangUpReward",
        "Information", "Item", "ItemCommonBuy", "ItemTreasure", "ItemTreasureDropList",
        "LoveDiary", "LovePointLevel", "LovePointReward", "MainLineTask", "MainUIAction",
        "MainUIActorInfo", "MainUIActorState", "MainUIBGM", "MainUIBGMSwitchActivity",
        "MainUIScene", "MainUIScenePlace", "MainUISpEvent", "MiaoGachaPackLibrary",
        "MonthlyCard", "MyWeapon", "PersonalInfoStyle", "PhoneAvatarMale",
        "PhoneAvatarPlayer", "PhoneCall", "PhoneContact", "PhoneMomentCover",
        "PhoneMoment", "PhoneMomentReply", "PhoneMsg", "PhoneMsgBubble", "PhoneMsgConversation",
        "PhoneMsgConversationCondition", "EasterEgg", "PhotoAction",
        "PhoneOfficialArticle", "PhoneSignature", "PhotoGroup", "PlayerLevel", "PlayerTag",
        "RadioReward", "RoleInfo", "SCoreBaseInfo", "ScoreSet", "ShareInfo", "ShareReward",
        "ShareRewardGroup", "ShopGroup", "SundryConfig", "SystemMail", "SystemUnLock", "Task", "WeaponSet",
        "WorldInfoList", "X3ActorCfgs", "X3WeaponLogicConfigs", "X3WeaponSkinConfigs",
    }
)

RUNTIME_PRESETS = {"booi": BOOI_RUNTIME_TABLES}


def runtime_tables(preset: str | None, additional: list[str]) -> list[str]:
    selected = set(additional)
    if preset:
        selected.update(RUNTIME_PRESETS[preset])
    return sorted(selected)
