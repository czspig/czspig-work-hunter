
from bosshunter.ai import resume


BASE = """# 李晓｜内容运营
联系：13912345678｜li@example.com
作品：https://example.com/work
## 职业经历
### 甲科技有限公司｜运营
- 内容策划。
### 乙公司｜传播
- 直播执行。
## 教育背景
### 示例大学｜本科
2018.09-2022.06
"""


def test_contacts_outside_basic_info_section_cannot_disappear():
    candidate = BASE.replace("联系：13912345678｜li@example.com\n", "").replace("作品：https://example.com/work\n", "")
    missing = resume._find_missing_core_facts(candidate, BASE)
    assert set(missing) == {"13912345678", "li@example.com", "https://example.com/work"}


def test_name_cannot_disappear():
    issues = resume._find_blocking_integrity_issues(BASE.replace("李晓", "其他人"), BASE)
    assert any("姓名" in issue for issue in issues)


def test_document_titles_do_not_override_explicit_name():
    for title in ("个人简历", "简历", "李晓的简历", "李晓 - 产品经理", "姓名：李晓"):
        source = BASE.replace("# 李晓｜内容运营", f"# {title}\n姓名：李晓")
        assert resume._find_blocking_integrity_issues(BASE, source) == []
        assert any("姓名" in issue for issue in resume._find_blocking_integrity_issues(BASE.replace("李晓", "其他人"), source))


def test_generic_title_without_explicit_name_is_not_a_person():
    source = BASE.replace("# 李晓｜内容运营", "# 个人简历")
    assert resume._find_blocking_integrity_issues(BASE, source) == []


def test_school_prose_can_be_reformatted_without_losing_identity():
    for prefix in ("毕业于", "就读于", "毕业院校：", "学校："):
        source = BASE.replace("### 示例大学｜本科", f"{prefix}示例大学，本科。")
        assert resume._find_blocking_integrity_issues(BASE, source) == []
        assert any("示例大学" in issue for issue in resume._find_blocking_integrity_issues(BASE.replace("示例大学", "另一大学"), source))


def test_section_aliases_and_reordering_preserve_background():
    candidate = BASE.replace("职业经历", "工作经验").replace("教育背景", "教育经历")
    assert resume._find_blocking_integrity_issues(candidate, BASE) == []


def test_combined_education_skills_section_and_inline_school_are_valid():
    candidate = BASE.replace("教育背景", "教育与技能").replace("### 示例大学｜本科\n2018.09-2022.06", "示例大学｜本科｜2018.09-2022.06")
    assert resume._find_blocking_integrity_issues(candidate, BASE) == []


def test_school_details_can_be_in_heading():
    candidate = BASE.replace("### 示例大学｜本科\n2018.09-2022.06", "### 示例大学｜本科｜2018.09-2022.06")
    assert resume._find_blocking_integrity_issues(candidate, BASE) == []


def test_project_link_is_not_mistaken_for_required_contact():
    source = BASE + "\n## 项目经历\n### 示例项目\n- 内容策划 https://example.com/project\n"
    candidate = source.replace(" https://example.com/project", "")
    assert resume._find_missing_core_facts(candidate, source) == []


def test_missing_employer_is_blocked_without_restricting_role_wording():
    candidate = BASE.replace("### 乙公司｜传播\n- 直播执行。\n", "")
    assert any("乙公司" in issue for issue in resume._find_blocking_integrity_issues(candidate, BASE))
    assert resume._find_blocking_integrity_issues(BASE.replace("直播执行", "主导直播执行"), BASE) == []


def test_education_content_and_dates_cannot_disappear():
    for candidate in (BASE.split("## 教育背景")[0], BASE.replace("2018.09-2022.06", ""), BASE.replace("本科", "")):
        assert any("教育" in issue for issue in resume._find_blocking_integrity_issues(candidate, BASE))


def test_empty_education_heading_is_not_complete():
    candidate = BASE.split("## 教育背景")[0] + "## 教育经历\n"
    assert any("缺少或清空教育经历" in issue for issue in resume._find_blocking_integrity_issues(candidate, BASE))
