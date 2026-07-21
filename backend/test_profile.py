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


from clients.claude import _profile_block
from agent.schemas import UserProfile


def test_profile_block_empty_for_none():
    assert _profile_block(None) == ""


def test_profile_block_empty_when_all_fields_blank():
    assert _profile_block(UserProfile(email="   ", notes="")) == ""


def test_profile_block_includes_populated_fields_and_notes():
    p = UserProfile(fullName="Ada Lovelace", email="ada@example.com",
                    phone="", notes="prefers window seats")
    block = _profile_block(p)
    assert "User's saved info" in block
    assert "Ada Lovelace" in block
    assert "ada@example.com" in block
    assert "prefers window seats" in block
    # empty phone is omitted, and its label must not appear
    assert "Phone" not in block


def test_profile_block_instructs_type_action_and_forbids_invention():
    block = _profile_block(UserProfile(email="ada@example.com"))
    assert "type" in block
    assert "never invent" in block.lower()
