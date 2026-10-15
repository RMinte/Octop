import { useMemo, useState } from "react";
import {
  Button,
  Empty,
  Input,
  List,
  Segmented,
  Space,
  Tag,
  Typography,
} from "antd";
import { Plus, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import type {
  ProjectResources,
  ProjectSkill,
} from "../../api/modules/projects";
import CatalogDrawer from "../Experts/components/CatalogDrawer";
import SkillHubTab from "../Agent/Skills/components/SkillHubTab";
import type { SkillHubSkill } from "../Agent/Skills/components/SkillHubDetailDrawer";
import styles from "./index.module.less";

export function projectSkillKey(skill: ProjectSkill): string {
  return JSON.stringify(
    "source" in skill
      ? ["skillhub", skill.slug]
      : ["package", skill.package_id, skill.slug],
  );
}

interface Props {
  value?: ProjectSkill[];
  onChange?: (value: ProjectSkill[]) => void;
  installed: ProjectResources["skills"];
  disabled?: boolean;
  active: boolean;
  id?: string;
}
const EMPTY: ProjectSkill[] = [];

export default function ProjectSkillsField({
  value = EMPTY,
  onChange,
  installed,
  disabled,
  active,
  id,
}: Props) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<"hub" | "installed">("hub");
  const [query, setQuery] = useState("");
  const keys = new Set(value.map(projectSkillKey));
  const pickedSlugs = useMemo(
    () =>
      new Set(
        value.filter((skill) => "source" in skill).map((skill) => skill.slug),
      ),
    [value],
  );
  const skillName = (skill: ProjectSkill) =>
    "source" in skill
      ? skill.name
      : installed.find(
          (entry) => projectSkillKey(entry) === projectSkillKey(skill),
        )?.name ?? skill.slug;
  const append = (skill: ProjectSkill) => {
    if (!disabled && !keys.has(projectSkillKey(skill)) && value.length < 64)
      onChange?.([...value, skill]);
  };
  function pickHub(skill: SkillHubSkill) {
    append({
      source: "skillhub",
      slug: skill.slug,
      name: skill.name,
      description: skill.description_zh || skill.description || "",
    });
  }
  const local = installed.filter((skill) =>
    `${skill.name} ${skill.slug} ${skill.description}`
      .toLowerCase()
      .includes(query.trim().toLowerCase()),
  );

  return (
    <div id={id}>
      {value.length > 0 && (
        <div className={styles.skillList}>
          {value.map((skill) => (
            <div key={projectSkillKey(skill)} className={styles.skillRow}>
              <div className={styles.skillMeta}>
                <Typography.Text strong>{skillName(skill)}</Typography.Text>
                <Typography.Text type="secondary">{skill.slug}</Typography.Text>
              </div>
              <Tag>
                {"source" in skill ? "SkillHub" : t("projects.installedSkills")}
              </Tag>
              <Button
                type="text"
                size="small"
                disabled={disabled}
                icon={<X size={15} />}
                aria-label={t("projects.removeSkill", {
                  name: skillName(skill),
                })}
                onClick={() =>
                  onChange?.(
                    value.filter(
                      (item) =>
                        projectSkillKey(item) !== projectSkillKey(skill),
                    ),
                  )
                }
              />
            </div>
          ))}
        </div>
      )}
      <Button
        icon={<Plus size={15} />}
        disabled={disabled || value.length >= 64}
        onClick={() => {
          setTab("hub");
          setOpen(true);
        }}
      >
        {t("projects.addSkill")}
      </Button>
      <CatalogDrawer
        title={t("projects.skillPickerTitle")}
        open={active && open}
        onClose={() => setOpen(false)}
      >
        <Segmented
          block
          value={tab}
          onChange={(next) => setTab(next as "hub" | "installed")}
          options={[
            { label: t("experts.addSkillFromHub"), value: "hub" },
            { label: t("projects.installedSkills"), value: "installed" },
          ]}
          style={{ marginBottom: 16, flexShrink: 0 }}
        />
        <div className={styles.skillCatalog}>
          {tab === "hub" ? (
            <SkillHubTab
              target={{ type: "browse" }}
              onPick={pickHub}
              pickedSlugs={pickedSlugs}
            />
          ) : (
            <>
              <Input.Search
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                allowClear
                placeholder={t("projects.searchInstalledSkills")}
              />
              {local.length ? (
                <List
                  dataSource={local}
                  renderItem={(skill) => (
                    <List.Item
                      key={projectSkillKey(skill)}
                      actions={[
                        <Button
                          key="pick"
                          disabled={
                            disabled ||
                            keys.has(projectSkillKey(skill)) ||
                            value.length >= 64
                          }
                          onClick={() =>
                            append({
                              package_id: skill.package_id,
                              slug: skill.slug,
                            })
                          }
                        >
                          {t(
                            keys.has(projectSkillKey(skill))
                              ? "experts.skillAlreadySelected"
                              : "experts.pickSkill",
                          )}
                        </Button>,
                      ]}
                    >
                      <List.Item.Meta
                        title={skill.name}
                        description={skill.description || skill.slug}
                      />
                    </List.Item>
                  )}
                />
              ) : (
                <Empty description={t("projects.noInstalledSkills")} />
              )}
            </>
          )}
        </div>
        <div className={styles.skillPickerFooter}>
          <Typography.Text type="secondary">
            {t("projects.selectedSkills", { count: value.length })}
          </Typography.Text>
          <Space>
            <Button type="primary" onClick={() => setOpen(false)}>
              {t("projects.finishSkillSelection")}
            </Button>
          </Space>
        </div>
      </CatalogDrawer>
    </div>
  );
}
