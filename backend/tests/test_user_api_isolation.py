import unittest
from unittest.mock import patch

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import current_user_id
from app.database import Base, get_db
from app.main import app
from app.models import TutorialStep, UserStepProgress


class UserApiIsolationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

        def test_db():
            db = self.sessions()
            try:
                yield db
            finally:
                db.close()

        def test_user(request: Request):
            return request.headers["x-test-user"]

        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[current_user_id] = test_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.client.close()
        self.engine.dispose()

    def headers(self, user_id: str) -> dict[str, str]:
        return {"x-test-user": user_id}

    def test_two_users_share_video_but_not_save_or_step_state(self):
        payload = {
            "url": "https://www.youtube.com/watch?v=shared123",
            "source_platform": "youtube",
            "source_video_id": "shared123",
            "title": "共享教程",
        }
        with patch("app.main._submit_download"):
            first = self.client.post("/api/videos", json=payload, headers=self.headers("user-a"))
            second = self.client.post("/api/videos", json=payload, headers=self.headers("user-b"))
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        video_id = first.json()["id"]
        self.assertEqual(second.json()["id"], video_id)

        saved = self.client.post(
            f"/api/videos/{video_id}/save", headers=self.headers("user-a")
        )
        self.assertTrue(saved.json()["saved"])
        self.assertTrue(
            self.client.get(f"/api/videos/{video_id}", headers=self.headers("user-a")).json()["saved"]
        )
        self.assertFalse(
            self.client.get(f"/api/videos/{video_id}", headers=self.headers("user-b")).json()["saved"]
        )

        with self.sessions() as db:
            step = TutorialStep(
                video_id=video_id,
                position=0,
                title="切菜",
                summary="切成小块",
                question="大小均匀吗？",
                criteria="大小基本一致",
            )
            db.add(step)
            db.commit()
            step_id = step.id

        first_progress = self.client.patch(
            f"/api/videos/{video_id}/steps/{step_id}",
            json={"done": True, "user_note": "甲的备注"},
            headers=self.headers("user-a"),
        )
        second_progress = self.client.patch(
            f"/api/videos/{video_id}/steps/{step_id}",
            json={"user_note": "乙的备注"},
            headers=self.headers("user-b"),
        )
        self.assertTrue(first_progress.json()["done"])
        self.assertEqual(first_progress.json()["user_note"], "甲的备注")
        self.assertFalse(second_progress.json()["done"])
        self.assertEqual(second_progress.json()["user_note"], "乙的备注")

        with self.sessions() as db:
            rows = db.scalars(
                select(UserStepProgress).where(UserStepProgress.step_id == step_id)
            ).all()
            self.assertEqual({row.user_id for row in rows}, {"user-a", "user-b"})


if __name__ == "__main__":
    unittest.main()
