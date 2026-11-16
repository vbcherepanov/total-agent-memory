from __future__ import annotations

GENERAL_PROJECT = 'general'


def normalize_rule_project(project: str | None) -> str:
    if project is None:
        return GENERAL_PROJECT
    if not isinstance(project, str):
        raise ValueError('project must be text or null')
    return project.strip() or GENERAL_PROJECT


def automatic_rule_scope(project: str | None) -> str:
    project = normalize_rule_project(project)
    return 'global' if project == GENERAL_PROJECT else f'project:{project}'
