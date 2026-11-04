import json


def build_log_time_modal(user_id, channel_id, default_date, issue_key=""):
    return {
        "type": "modal", "callback_id": "log_time_submit",
        "private_metadata": json.dumps({"user_id": user_id, "channel_id": channel_id}),
        "title": {"type": "plain_text", "text": "Log Time"},
        "submit": {"type": "plain_text", "text": "Log Time"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type":"input","block_id":"issue_block","label":{"type":"plain_text","text":"Issue"},"element":{"type":"plain_text_input","action_id":"issue_key","initial_value":issue_key,"placeholder":{"type":"plain_text","text":"PROJ-123"}}},
            {"type":"input","block_id":"date_block","label":{"type":"plain_text","text":"Date"},"element":{"type":"datepicker","action_id":"work_date","initial_date":default_date}},
            {"type":"input","block_id":"duration_block","label":{"type":"plain_text","text":"Duration"},"element":{"type":"plain_text_input","action_id":"duration","placeholder":{"type":"plain_text","text":"1h 30m"}}},
            {"type":"input","block_id":"start_time_block","label":{"type":"plain_text","text":"Start Time"},"element":{"type":"timepicker","action_id":"start_time","initial_time":"09:00"}},
            {"type":"input","block_id":"description_block","optional":True,"label":{"type":"plain_text","text":"Description"},"element":{"type":"plain_text_input","action_id":"description","multiline":True}},
            {"type":"input","block_id":"estimate_block","label":{"type":"plain_text","text":"Remaining estimate"},"element":{"type":"static_select","action_id":"remaining_estimate","initial_option":{"text":{"type":"plain_text","text":"Use existing estimate"},"value":"leave"},"options":[{"text":{"type":"plain_text","text":"Use existing estimate"},"value":"leave"},{"text":{"type":"plain_text","text":"Adjust automatically"},"value":"auto"},{"text":{"type":"plain_text","text":"Set a new estimate"},"value":"new"}]}},
            {"type":"input","block_id":"new_estimate_block","optional":True,"label":{"type":"plain_text","text":"New remaining estimate (if selected above)"},"element":{"type":"plain_text_input","action_id":"new_estimate","placeholder":{"type":"plain_text","text":"e.g. 2h or 1d"}}},
        ],
    }


def get_modal_form(view):
    values = view["state"]["values"]
    def value(block, action):
        return values[block][action].get("value")
    return {
        "issue_key": (value("issue_block", "issue_key") or "").strip().upper(),
        "date": values["date_block"]["work_date"].get("selected_date"),
        "duration": (value("duration_block", "duration") or "").strip(),
        "start_time": values["start_time_block"]["start_time"].get("selected_time"),
        "description": (value("description_block", "description") or "").strip(),
        "remaining_estimate": values["estimate_block"]["remaining_estimate"]["selected_option"]["value"],
        "new_estimate": (value("new_estimate_block", "new_estimate") or "").strip(),
    }
