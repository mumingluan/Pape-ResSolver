from __future__ import annotations

# Keep this list synchronized with the configuration tables decoded by
# Pape-BOOI/internal/gamedata. It is intentionally explicit: a new server
# dependency must be reviewed before the runtime database format changes.
BOOI_RUNTIME_TABLES = frozenset(
    {
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
        "PhoneMomentReply", "PhoneMsg", "PhoneMsgBubble", "PhoneMsgConversation",
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
