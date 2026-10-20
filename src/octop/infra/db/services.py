"""Database services — RepoBundle and SharedServices DI container."""

from __future__ import annotations

from dataclasses import dataclass

from octop.config import OctopConfig
from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.audit import AuditRepo
from octop.infra.db.repos.backends import BackendRepo
from octop.infra.db.repos.bridge_connections import BridgeConnectionRepo
from octop.infra.db.repos.care_push import CarePushRepo
from octop.infra.db.repos.channels import ChannelRepo
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.db.repos.cron import CronJobRepo
from octop.infra.db.repos.external_imports import ExternalImportRepo
from octop.infra.db.repos.invites import InviteRepo
from octop.infra.db.repos.knowledge import KnowledgeRepo
from octop.infra.db.repos.proactive_care_config import ProactiveCareConfigRepo
from octop.infra.db.repos.project_sync import ProjectSyncRepo
from octop.infra.db.repos.project_task_artifacts import ProjectTaskArtifactRepo
from octop.infra.db.repos.project_task_attachments import ProjectTaskAttachmentRepo
from octop.infra.db.repos.project_task_execution_receipts import ProjectTaskExecutionReceiptRepo
from octop.infra.db.repos.project_task_reviews import ProjectTaskReviewRepo
from octop.infra.db.repos.project_task_titles import ProjectTaskTitleRepo
from octop.infra.db.repos.project_task_tool_calls import ProjectTaskToolCallRepo
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRepo
from octop.infra.db.repos.project_tasks import ProjectTaskRepo
from octop.infra.db.repos.projects import ProjectRepo
from octop.infra.db.repos.providers import ProviderRepo
from octop.infra.db.repos.published_experts import PublishedExpertRepo
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.db.repos.sessions import SessionRepo
from octop.infra.db.repos.settings import SettingsRepo
from octop.infra.db.repos.skill_packages import SkillPackageRepo
from octop.infra.db.repos.sso import SsoRepo
from octop.infra.db.repos.thread_messages import ThreadMessageRepo
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.repos.trajectory_events import TrajectoryEventRepo
from octop.infra.db.repos.usage import UsageRepo
from octop.infra.db.repos.user_policies import UserPolicyRepo
from octop.infra.db.repos.user_task_approval_policies import UserTaskApprovalPolicyRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.repos.voice_providers import VoiceProviderRepo
from octop.infra.utils.paths import PathLayout


@dataclass(frozen=True)
class RepoBundle:
    db: DatabasePool

    project_sync_repo: ProjectSyncRepo
    project_task_repo: ProjectTaskRepo
    user_task_approval_policy_repo: UserTaskApprovalPolicyRepo
    project_task_title_repo: ProjectTaskTitleRepo
    project_task_review_repo: ProjectTaskReviewRepo
    project_task_artifact_repo: ProjectTaskArtifactRepo
    project_task_attachment_repo: ProjectTaskAttachmentRepo
    project_task_execution_receipt_repo: ProjectTaskExecutionReceiptRepo
    project_task_tool_call_repo: ProjectTaskToolCallRepo
    project_task_turn_repo: ProjectTaskTurnRepo
    project_repo: ProjectRepo
    external_import_repo: ExternalImportRepo
    user_repo: UserRepo
    user_policy_repo: UserPolicyRepo
    invite_repo: InviteRepo
    agent_repo: AgentRepo
    provider_repo: ProviderRepo
    channel_repo: ChannelRepo
    cron_repo: CronJobRepo
    session_repo: SessionRepo
    thread_repo: ThreadRepo
    thread_message_repo: ThreadMessageRepo
    trajectory_event_repo: TrajectoryEventRepo
    secret_repo: SecretRepo
    audit_repo: AuditRepo
    usage_repo: UsageRepo
    settings_repo: SettingsRepo
    storage_backend_repo: BackendRepo
    bridge_connection_repo: BridgeConnectionRepo
    connector_repo: ConnectorRepo
    skill_package_repo: SkillPackageRepo
    published_expert_repo: PublishedExpertRepo
    knowledge_repo: KnowledgeRepo
    voice_provider_repo: VoiceProviderRepo
    care_push_repo: CarePushRepo
    proactive_care_config_repo: ProactiveCareConfigRepo
    sso_repo: SsoRepo

    @classmethod
    def from_pool(cls, db: DatabasePool) -> RepoBundle:
        return cls(
            db=db,
            project_sync_repo=ProjectSyncRepo(),
            project_task_repo=ProjectTaskRepo(db),
            user_task_approval_policy_repo=UserTaskApprovalPolicyRepo(),
            project_task_title_repo=ProjectTaskTitleRepo(db),
            project_task_review_repo=ProjectTaskReviewRepo(db),
            project_task_turn_repo=ProjectTaskTurnRepo(db),
            project_task_tool_call_repo=ProjectTaskToolCallRepo(),
            project_task_artifact_repo=ProjectTaskArtifactRepo(),
            project_task_attachment_repo=ProjectTaskAttachmentRepo(),
            project_task_execution_receipt_repo=ProjectTaskExecutionReceiptRepo(),
            project_repo=ProjectRepo(db),
            external_import_repo=ExternalImportRepo(),
            user_repo=UserRepo(db),
            user_policy_repo=UserPolicyRepo(db),
            invite_repo=InviteRepo(db),
            agent_repo=AgentRepo(db),
            provider_repo=ProviderRepo(db),
            channel_repo=ChannelRepo(db),
            cron_repo=CronJobRepo(db),
            session_repo=SessionRepo(db),
            thread_repo=ThreadRepo(db),
            thread_message_repo=ThreadMessageRepo(db),
            trajectory_event_repo=TrajectoryEventRepo(db),
            secret_repo=SecretRepo(db),
            audit_repo=AuditRepo(db),
            usage_repo=UsageRepo(db),
            settings_repo=SettingsRepo(db),
            storage_backend_repo=BackendRepo(db),
            bridge_connection_repo=BridgeConnectionRepo(db),
            connector_repo=ConnectorRepo(db),
            skill_package_repo=SkillPackageRepo(db),
            published_expert_repo=PublishedExpertRepo(db),
            knowledge_repo=KnowledgeRepo(db),
            voice_provider_repo=VoiceProviderRepo(db),
            care_push_repo=CarePushRepo(db),
            proactive_care_config_repo=ProactiveCareConfigRepo(db),
            sso_repo=SsoRepo(db),
        )


@dataclass(frozen=True)
class SharedServices:
    paths: PathLayout
    config: OctopConfig
    repos: RepoBundle

    @property
    def db(self) -> DatabasePool:
        return self.repos.db

    @property
    def project_sync_repo(self) -> ProjectSyncRepo:
        return self.repos.project_sync_repo

    @property
    def project_task_repo(self) -> ProjectTaskRepo:
        return self.repos.project_task_repo

    @property
    def user_task_approval_policy_repo(self) -> UserTaskApprovalPolicyRepo:
        return self.repos.user_task_approval_policy_repo

    @property
    def project_task_title_repo(self) -> ProjectTaskTitleRepo:
        return self.repos.project_task_title_repo

    @property
    def project_task_review_repo(self) -> ProjectTaskReviewRepo:
        return self.repos.project_task_review_repo

    @property
    def project_task_artifact_repo(self) -> ProjectTaskArtifactRepo:
        return self.repos.project_task_artifact_repo

    @property
    def project_task_attachment_repo(self) -> ProjectTaskAttachmentRepo:
        return self.repos.project_task_attachment_repo

    @property
    def project_task_tool_call_repo(self) -> ProjectTaskToolCallRepo:
        return self.repos.project_task_tool_call_repo

    @property
    def project_task_execution_receipt_repo(self) -> ProjectTaskExecutionReceiptRepo:
        return self.repos.project_task_execution_receipt_repo

    @property
    def project_task_turn_repo(self) -> ProjectTaskTurnRepo:
        return self.repos.project_task_turn_repo

    @property
    def project_repo(self) -> ProjectRepo:
        return self.repos.project_repo

    @property
    def external_import_repo(self) -> ExternalImportRepo:
        return self.repos.external_import_repo

    @property
    def user_repo(self) -> UserRepo:
        return self.repos.user_repo

    @property
    def user_policy_repo(self) -> UserPolicyRepo:
        return self.repos.user_policy_repo

    @property
    def invite_repo(self) -> InviteRepo:
        return self.repos.invite_repo

    @property
    def agent_repo(self) -> AgentRepo:
        return self.repos.agent_repo

    @property
    def provider_repo(self) -> ProviderRepo:
        return self.repos.provider_repo

    @property
    def channel_repo(self) -> ChannelRepo:
        return self.repos.channel_repo

    @property
    def cron_repo(self) -> CronJobRepo:
        return self.repos.cron_repo

    @property
    def session_repo(self) -> SessionRepo:
        return self.repos.session_repo

    @property
    def thread_repo(self) -> ThreadRepo:
        return self.repos.thread_repo

    @property
    def thread_message_repo(self) -> ThreadMessageRepo:
        return self.repos.thread_message_repo

    @property
    def trajectory_event_repo(self) -> TrajectoryEventRepo:
        return self.repos.trajectory_event_repo

    @property
    def secret_repo(self) -> SecretRepo:
        return self.repos.secret_repo

    @property
    def audit_repo(self) -> AuditRepo:
        return self.repos.audit_repo

    @property
    def usage_repo(self) -> UsageRepo:
        return self.repos.usage_repo

    @property
    def settings_repo(self) -> SettingsRepo:
        return self.repos.settings_repo

    @property
    def storage_backend_repo(self) -> BackendRepo:
        return self.repos.storage_backend_repo

    @property
    def bridge_connection_repo(self) -> BridgeConnectionRepo:
        return self.repos.bridge_connection_repo

    @property
    def connector_repo(self) -> ConnectorRepo:
        return self.repos.connector_repo

    @property
    def skill_package_repo(self) -> SkillPackageRepo:
        return self.repos.skill_package_repo

    @property
    def published_expert_repo(self) -> PublishedExpertRepo:
        return self.repos.published_expert_repo

    @property
    def knowledge_repo(self) -> KnowledgeRepo:
        return self.repos.knowledge_repo

    @property
    def voice_provider_repo(self) -> VoiceProviderRepo:
        return self.repos.voice_provider_repo

    @property
    def care_push_repo(self) -> CarePushRepo:
        return self.repos.care_push_repo

    @property
    def proactive_care_config_repo(self) -> ProactiveCareConfigRepo:
        return self.repos.proactive_care_config_repo

    @property
    def sso_repo(self) -> SsoRepo:
        return self.repos.sso_repo


def build_shared_services(
    *, db: DatabasePool, paths: PathLayout, config: OctopConfig
) -> SharedServices:
    return SharedServices(
        paths=paths,
        config=config,
        repos=RepoBundle.from_pool(db),
    )
