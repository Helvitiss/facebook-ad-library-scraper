from aiogram.fsm.state import State, StatesGroup

class KWState(StatesGroup):
    """Состояния FSM для процесса извлечения ключевых слов."""
    waiting_for_url = State()
