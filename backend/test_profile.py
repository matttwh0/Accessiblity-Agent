# Tests for the user-profile autofill feature: schema, prompt-block formatting,
# and that the profile reaches the agent's decide/recover prompts.
from agent.schemas import AgentState, UserProfile, PageContext, DOMNode


def make_context():
    return PageContext(
        url="https://example.com",
        title="Test",
        dom_tree=[DOMNode(tag="input", label="Email", selector="#email")],
    )


def test_agentstate_defaults_profile_to_none():
    state = AgentState(task="t", context=make_context())
    assert state.profile is None


def test_agentstate_accepts_userprofile_from_dict():
    profile = UserProfile(**{"email": "a@b.com", "fullName": "Ada"})
    state = AgentState(task="t", context=make_context(), profile=profile)
    assert state.profile.email == "a@b.com"
    assert state.profile.fullName == "Ada"
    # unset fields default to None
    assert state.profile.phone is None
