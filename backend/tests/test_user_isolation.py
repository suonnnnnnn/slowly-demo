import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auth import token_hash
from app.database import Base
from app.models import TutorialStep, User, UserStepProgress, UserTutorial, Video, utcnow


class UserIsolationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_shared_tutorial_keeps_private_save_and_progress(self):
        with Session(self.engine) as db:
            first = User()
            second = User()
            video = Video(source_url="https://www.youtube.com/watch?v=shared", status="ready")
            db.add_all([first, second, video])
            db.flush()
            step = TutorialStep(
                video_id=video.id,
                position=0,
                title="切菜",
                summary="切成小块",
                question="大小均匀吗？",
                criteria="大小基本一致",
            )
            db.add(step)
            db.flush()
            db.add_all([
                UserTutorial(user_id=first.id, video_id=video.id, saved_at=utcnow()),
                UserTutorial(user_id=second.id, video_id=video.id),
                UserStepProgress(
                    user_id=first.id,
                    video_id=video.id,
                    step_id=step.id,
                    done=True,
                    user_note="我的备注",
                ),
            ])
            db.commit()

            first_link = db.scalar(select(UserTutorial).where(UserTutorial.user_id == first.id))
            second_link = db.scalar(select(UserTutorial).where(UserTutorial.user_id == second.id))
            second_progress = db.scalar(
                select(UserStepProgress).where(
                    UserStepProgress.user_id == second.id,
                    UserStepProgress.step_id == step.id,
                )
            )

            self.assertIsNotNone(first_link.saved_at)
            self.assertIsNone(second_link.saved_at)
            self.assertIsNone(second_progress)

    def test_session_storage_uses_a_digest(self):
        token = "private-browser-token"
        digest = token_hash(token)
        self.assertNotEqual(digest, token)
        self.assertEqual(len(digest), 64)


if __name__ == "__main__":
    unittest.main()
