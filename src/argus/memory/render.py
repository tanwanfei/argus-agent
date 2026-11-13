from argus.memory.records import Episode, Fact, Habit, MemoryItem, MemoryKind


def fact_item(fact: Fact) -> MemoryItem:
    return MemoryItem(MemoryKind.FACT, f"{fact.subject}的{fact.attribute}：{fact.value}")


_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def episode_item(episode: Episode) -> MemoryItem:
    date_text = f"{episode.ts:%Y-%m-%d} {_WEEKDAYS[episode.ts.weekday()]}"
    happened_on = episode.ts.date()
    said_on = episode.created_at.astimezone(episode.ts.tzinfo).date()
    if said_on < happened_on:
        said_note = f"（{said_on:%Y-%m-%d} 说的打算）"
    elif said_on > happened_on:
        said_note = f"（{said_on:%Y-%m-%d} 说的）"
    else:
        said_note = ""
    return MemoryItem(MemoryKind.EPISODE, f"{date_text} {episode.summary}{said_note}")


def habit_item(habit: Habit) -> MemoryItem:
    return MemoryItem(MemoryKind.HABIT, habit.statement)
