from pathlib import Path
WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
import yaml

# ==============================================================================
# -- 技能目录与动态加载器 (Skill catalog & Loader) --
# ==============================================================================
class SkillLoader:
    """技能扫描与加载器，负责扫描 Markdown 文件的 Frontmatter 元信息并按需读取正文"""

    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills: dict[str, dict[str, str]] = {}
        self.scan()

    @staticmethod
    def parse_frontmatter(text: str) -> tuple[dict, str]:
        """
        解析 Markdown 顶部的 YAML Frontmatter（格式为由 '---' 包裹的键值对）。
        返回: (metadata_dict, markdown_body)
        """
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].rstrip("\r\n") != "---":
            return {}, text

        # 查找闭合的 '---'
        closing_index = next(
            (index for index, line in enumerate(lines[1:], start=1)
             if line.rstrip("\r\n") == "---"),
            None,
        )
        if closing_index is None:
            return {}, text

        frontmatter = "".join(lines[1:closing_index])
        body = "".join(lines[closing_index + 1:]).strip()
        try:
            metadata = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return metadata, body

    def scan(self):
        """扫描 skills 目录下所有一级子目录中的 SKILL.md 文件"""
        self.skills.clear()
        if not self.skills_dir.exists():
            return
        skills_root = self.skills_dir.resolve()

        for manifest in sorted(self.skills_dir.glob("*/SKILL.md")):
            # 安全检查：确保是真实文件且没有通过软链接逃逸出 skills 根目录
            if (not manifest.is_file()
                    or not manifest.resolve().is_relative_to(skills_root)):
                continue

            content = manifest.read_text(encoding="utf-8")
            metadata, body = self.parse_frontmatter(content)
            # 解析技能名称：优先取 frontmatter 中的 name，降级取所在文件夹名
            raw_name = metadata.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            name = name or manifest.parent.name

            # 解析描述：优先取 frontmatter 中的 description，降级取正文第一行
            raw_description = metadata.get("description")
            description = (raw_description.strip()
                           if isinstance(raw_description, str) else "")
            description = description or body.split("\n", 1)[0]
            description = " ".join(str(description).lstrip("# ").split())

            self.skills[name] = {
                "name": name,
                "description": description,
                "content": content,  # 完整 Markdown 内容，供动态加载
            }

    def catalog(self) -> str :
        """生成供 System Prompt 使用的轻量级摘要目录"""
        if not self.skills:
            return "(no skills found)"
        return "\n".join(
            f"- {skill['name']}: {skill['description']}"
            for skill in self.skills.values()
         )

    def load(self, name: str) -> str:
        """根据技能名称返回对应的完整 SKILL.md 内容（供 Tool 调用）"""
        skill = self.skills.get(name)
        if skill:
            return skill["content"]
        available = ", ".join(self.skills) or "none"
        return f"Error: Unknown skill '{name}'. Available: {available}"

SKILL_LOADER = SkillLoader(SKILLS_DIR)