"""
fsm_conductor.py
~~~~~~~~~~~~~~~~
Декларативное описание графа и инициализация VM.
"""
from nano_vm import Program, ExecutionVM
from nano_vm.adapters import MockLLMAdapter
from .fsm_repository import SqliteCursorRepository
from .fsm_tools import (
    initiate_payment_wait, verify_webhook_data, fsm_transition_to_paid,
    create_download_token_tool, pay_referral_tool, send_tg_notification_tool, log_fraud_attempt
)

# Декларативный граф кондуктора
payment_conductor = Program.from```_dict({
    "name": "payment_webhook_pipeline",
    "steps": [
        {
            "id": "wait_for_payment",
            "type": "tool",
            "tool": "initiate_payment_wait"
        },
        {
            "id": "verify_payment",
            "type": "tool",
            "tool": "verify_webhook_data",
            "output_key": "payment_is_valid"
        },
        {
            "id": "guard_validity",
            "type": "condition",
            "condition": "$payment_is_valid == True",
            "then": "update_order_status",
            "otherwise": "log_fraud_attempt"
        },
        {
            "id": "update_order_status",
            "type": "tool",
            "tool": "fsm_transition_to_paid"
        },
        {
            "id": "post_payment_actions",
            "type": "parallel",
            "on_error": "skip",  # Падение уведомления не валит весь заказ
            "parallel_steps": [
                {"id": "issue_token", "type": "tool", "tool": "create_download_token_tool"},
                {"id": "pay_referral", "type": "tool", "tool": "pay_referral_tool"},
                {"id": "notify_tg", "type": "tool", "tool": "send_tg_notification_tool"}
            ]
        },
        {
            "id": "log_fraud_attempt",
            "type": "tool",
            "tool": "log_fraud_attempt"
        }
    ]
})

fsm_repo = SqliteCursorRepository()

# Глобальный инстанс виртуальной машины кондуктора
conductor_vm = ExecutionVM(
    llm=MockLLMAdapter("NO_LLM_IN_THIS_PROGRAM"), # Отключаем LLM
    tools={
        "initiate_payment_wait": initiate_payment_wait,
        "verify_webhook_data": verify_webhook_data,
        "fsm_transition_to_paid": fsm_transition_to_paid,
        "create_download_token_tool": create_download_token_tool,
        "pay_referral_tool": pay_referral_tool,
        "send_tg_notification_tool": send_tg_notification_tool,
        "log_fraud_attempt": log_fraud_attempt,
    },
    cursor_repo=fsm_repo
)
