"""Knowledge initialization role admission, shared by execution and planning."""
from .model_identity import model_family


def knowledge_init_role_error(stage, *, generator_provider, generator_model,
                              judge_provider, judge_model, unlimited_subscription=False,
                              generator_has_fallback=False, judge_has_fallback=False):
    """Return a policy refusal without probing accounts or dispatching models.

    Discovery requires different families even across providers. Unlimited init
    additionally pins its documented extraction/judgment pair. Authentication
    and availability remain execution checks.
    """
    same_family = model_family(generator_model) == model_family(judge_model)
    if stage == "feature-discovery" and same_family:
        return "feature discovery extraction and review must use independent model families"
    if unlimited_subscription:
        if generator_has_fallback or judge_has_fallback:
            return "--unlimited-subscription requires pinned generator and judge without fallback"
        if (generator_provider != "zcode" or generator_model.casefold() != "glm-5.3"
                or judge_provider != "codex" or same_family):
            return "--unlimited-subscription requires Zcode GLM-5.3 extraction and an independent Codex judge"
    return None
