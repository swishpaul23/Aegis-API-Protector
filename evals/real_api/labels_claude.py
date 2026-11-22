"""Claude's labels for REVIEW.md, applying the stated rules.

C = confirm as shown; str = fix column; None = unsure (left blank).
Run once: python evals/real_api/labels_claude.py
"""

import re
import sys

C = "CONFIRM"
L = {
    "box-FolderLocks-1e23b4a5": C,
    "box-FolderLocks-81bbc3a7": C,
    "box-FolderLocks-fe7877dd": C,
    # integration_type (partner app) != type (mapping type)
    "box-IntegrationMapping--Base-8d599d35": C,
    "box-IntegrationMappings-ee200d5d": C,
    "box-IntegrationMappings-fffdb6f4": C,
    "github-actions-billing-usage-7710a4f6": "; ".join(
        f"minutes_used_breakdown.linux_{n}_core<-minutes_used_breakdown.ubuntu_{n}_core"
        for n in (4, 8, 16, 32, 64)
    ),
    "github-copilot-organization-details-d275be1a": C,  # identical description
    "github-organization-secret-scanning-alert-3604719c": C,
    "github-organization-secret-scanning-alert-4e1838b1": C,
    "github-organization-secret-scanning-alert-4fc9328a": C,
    "github-organization-secret-scanning-alert-581e92a4": C,
    "github-organization-secret-scanning-alert-676a7958": C,
    "github-organization-secret-scanning-alert-fe2da873": C,
    # license/role name != bypasser's name
    "github-organization-secret-scanning-alert-578bede0": "REFUSE",
    # license node_id != user node_id
    "github-organization-secret-scanning-alert-6898c369": "REFUSE",
    "github-organization-secret-scanning-alert-75988e49": "REFUSE",
    "github-organization-secret-scanning-alert-9b300f44": "REFUSE",
    # repo admin permission != site_admin
    "github-organization-secret-scanning-alert-bd54c79a": "REFUSE",
    "github-rate-limit-overview-10c3e882": "REFUSE",
    "github-rate-limit-overview-47665090": "REFUSE",
    "github-rate-limit-overview-95992db3": "REFUSE",
    "github-rate-limit-overview-ce4e6170": "REFUSE",
    # stargazers may duplicate stargazers_count; unsure
    "github-webhook-discussion-transferred-a9bdb155": None,
    "github-webhook-discussion-transferred-b3fba3d7": "REFUSE",
    "github-webhook-discussion-unanswered-bbabc834": C,
    # base.repo.X could come from pull_request.X, head.repo.X cannot: unsure
    "github-webhook-pull-request-closed-177769dd": None,
    "github-webhook-pull-request-closed-31330bcb": None,
    "github-webhook-pull-request-closed-80c0275c": None,
    "github-webhook-pull-request-closed-9ceb32c3": None,
    "github-webhook-pull-request-closed-d4ee1b99": None,
    "stripe-account-a356f9e0": C,
    "stripe-account-af1f2562": C,  # verification -> requirements, same description
    "stripe-account-c1dff326": "REFUSE",  # display_name != individual first/last name
    "stripe-account-d3353680": "REFUSE",  # business_name/url != business_type
    "stripe-account_settings-6d1e7a5b": C,
    "stripe-error-6c2a9eaa": C,
    "stripe-error-ab24fb61": C,
    "stripe-error-cce6d891": C,
    "stripe-error-e33f7688": C,
    "stripe-error-f8b5ed94": C,
    "stripe-invoice-578c9459": C,
    "stripe-issuing.authorization-0ba685ec": C,
    # held vs authorized amount: not clearly the same
    "stripe-issuing.authorization-142d1744": None,
    "stripe-issuing.authorization-725cdfdd": (
        "authorized_currency<-merchant_currency; held_currency<-currency"
    ),
    "stripe-issuing.card-ddc42da3": C,
    "stripe-issuing.settlement-0442b8d5": C,
    "stripe-issuing.settlement-84d92438": C,
    "stripe-issuing.settlement-be9ad919": C,
    "stripe-issuing_authorization_verification_data-950a9fe3": C,
    "stripe-issuing_transaction_fuel_data-1fc52bcb": C,
    "stripe-order-87eca253": "REFUSE",
    "stripe-order-ddab93c2": "REFUSE",  # returns list != line_items list
    "stripe-order-d96adeda": C,
    "stripe-plan-64287624": "REFUSE",  # display name != internal nickname
    "stripe-source-2ea4e811": C,
    "stripe-source-6a64884d": "REFUSE",
    "stripe-source_transaction-0fe75008": "REFUSE",
    "stripe-source_transaction-355e051e": "REFUSE",
    "stripe-source_transaction-3ec3fcb3": C,
    "stripe-source_transaction-783b3c35": "REFUSE",
    "stripe-source_transaction-a6504d78": "REFUSE",
    "stripe-source_transaction-ceec42c1": C,
    "stripe-source_transaction_gbp_credit_transfer_data-095a9213": C,
    "stripe-subscription_schedule-5619fb80": C,
    "stripe-subscription_schedule-6eb60558": C,
    # BGN != GIP currency
    "stripe-terminal_configuration_configuration_resource_tipping-dd5d3895": "REFUSE",
    "twilio-events.v1.schema-3192c225": C,
    "twilio-events.v1.schema-a952aa19": C,
    "twilio-events.v1.subscription.subscribed_event-20fc2fa6": C,
    "twilio-iam.v1.CreateAccountRequest-aa0c08d8": C,
    # same description
    "twilio-messaging.v1.service.us_app_to_person-4d091840": "status<-campaign_status",
    "twilio-oauth.v1.token-0dc25a8f": C,
    "twilio-oauth.v1.token-abaf1b6b": C,  # absolute time != relative
    # same desc
    "twilio-trusthub.v1.compliance_tollfree_inquiry-efe598fa": "tollfree_id<-registration_id",
}


def main() -> None:
    p = "evals/real_api/REVIEW.md"
    lines = open(p, encoding="utf-8").read().split("\n")
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"\| `([^`]+)` \|", line)
        if not m:
            continue
        cid = m.group(1)
        seen.add(cid)
        if cid not in L:
            sys.exit(f"no label for {cid}")
        v = L[cid]
        if v is None:
            continue
        cells = line.split(" | ")
        assert cells[-3].startswith("[ ] confirm") and cells[-2].startswith("[ ] fix"), cells[-3:]
        if v == C:
            cells[-3] = "[x] confirm"
        else:
            cells[-1] = v + " |"
        lines[i] = " | ".join(cells)
    missing = set(L) - seen
    if missing:
        sys.exit(f"labels for unknown ids: {missing}")
    open(p, "w", encoding="utf-8").write("\n".join(lines))
    labeled = sum(v is not None for v in L.values())
    print("labeled", labeled, "blank", len(L) - labeled)


if __name__ == "__main__":
    main()
