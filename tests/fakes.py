from aegis_healer.agent import BudgetExceeded, Usage
from aegis_healer.types import FieldMapping, MappingPatch


class FakeProposer:
    """Returns a fixed patch; records requests it was asked about."""

    def __init__(self, patch: MappingPatch):
        self.patch = patch
        self.requests = []
        self.last_usage = Usage()

    def propose(self, request, check, budget=None):
        self.last_usage = Usage()
        if budget is not None and budget < 1:
            raise BudgetExceeded(["LLM call budget exhausted"])
        self.requests.append(request)
        self.last_usage = Usage(calls=1, input_tokens=1000, output_tokens=200)
        check(self.patch)
        return MappingPatch(
            mappings=[m.model_copy(update={"origin": "agent"}) for m in self.patch.mappings],
            unmappable=self.patch.unmappable,
        )


class FakeStructuredLLM:
    """Stands in for ChatAnthropic.with_structured_output: returns patches in order."""

    def __init__(self, patches):
        self.patches = list(patches)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return self.patches[min(len(self.calls) - 1, len(self.patches) - 1)]


def m(source, target, transform="none", confidence=0.95, **kw):
    return FieldMapping(
        source=source,
        target=target,
        transform=transform,
        confidence=confidence,
        origin="agent",
        **kw,
    )


def v2_agent_patch(confidence=0.95):
    return MappingPatch(
        mappings=[
            m("id", "user_id", "to_int", confidence),
            m("registered", "signup_date", "parse_datetime", confidence),
            m("address.zip", "address.postal_code", "none", confidence),
        ]
    )
