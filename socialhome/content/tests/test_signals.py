import json
from unittest.mock import patch, Mock, call

from socialhome.content.models import Tag
from socialhome.content.tests.factories import ContentFactory
from socialhome.enums import Visibility
from socialhome.federate.tasks import send_content, send_content_retraction, send_reply, send_share
from socialhome.notifications.tasks import send_reply_notifications, send_share_notification
from socialhome.tests.utils import SocialhomeTestCase, SocialhomeTransactionTestCase
from socialhome.users.tests.factories import UserFactory, ProfileFactory


class TestContentPostSave(SocialhomeTransactionTestCase):
    @patch("socialhome.content.signals.update_streams_with_content")
    def test_calls_update_streams_with_content(self, mock_update):
        # Calls on create
        content = ContentFactory()
        mock_update.assert_called_once_with(content)
        mock_update.reset_mock()
        # Does not call on update
        content.text = "update!"
        content.save()
        self.assertFalse(mock_update.called)


class TestNotifyListeners(SocialhomeTestCase):
    @patch("socialhome.content.signals.StreamConsumer")
    def test_content_save_calls_streamconsumer_group_send(self, mock_consumer):
        mock_consumer.group_send = Mock()
        # Public post no tags or followers
        content = ContentFactory()
        data = json.dumps({"event": "new", "id": content.id})
        calls = [
            call("streams_public", data),
            call("streams_profile__%s" % content.author.id, data),
            call("streams_profile_all__%s" % content.author.id, data),
        ]
        mock_consumer.group_send.assert_has_calls(calls, any_order=True)
        mock_consumer.group_send.reset_mock()
        # Private post with tags
        content = ContentFactory(visibility=Visibility.LIMITED, text="#foobar #barfoo")
        data = json.dumps({"event": "new", "id": content.id})
        calls = [
            call("streams_tag__%s_foobar" % Tag.objects.get(name="foobar").id, data),
            call("streams_tag__%s_barfoo" % Tag.objects.get(name="barfoo").id, data),
            call("streams_profile__%s" % content.author.id, data),
            call("streams_profile_all__%s" % content.author.id, data),
        ]
        mock_consumer.group_send.assert_has_calls(calls, any_order=True)
        mock_consumer.group_send.reset_mock()
        # Public post with followers
        follower = UserFactory()
        follower2 = UserFactory()
        profile = ProfileFactory()
        follower.profile.following.add(content.author)
        follower2.profile.following.add(content.author)
        profile.following.add(content.author)
        content = ContentFactory(author=content.author)
        data = json.dumps({"event": "new", "id": content.id})
        calls = [
            call("streams_public", data),
            call("streams_profile__%s" % content.author.id, data),
            call("streams_profile_all__%s" % content.author.id, data),
            call("streams_followed__%s" % follower.username, data),
            call("streams_followed__%s" % follower2.username, data),
        ]
        mock_consumer.group_send.assert_has_calls(calls, any_order=True)
        mock_consumer.group_send.reset_mock()
        # Replies
        reply = ContentFactory(parent=content)
        data = json.dumps({"event": "new", "id": reply.id})
        mock_consumer.group_send.assert_called_once_with("streams_content__%s" % content.channel_group_name, data)
        mock_consumer.group_send.reset_mock()
        # Update shouldn't cause a group send
        content.text = "foo"
        content.save()
        self.assertFalse(mock_consumer.group_send.called)


class TestFederateContent(SocialhomeTransactionTestCase):
    @patch("socialhome.content.signals.django_rq.enqueue")
    @patch("socialhome.content.signals.update_streams_with_content")
    def test_non_local_content_does_not_get_sent(self, mock_update, mock_send):
        ContentFactory()
        mock_send.assert_not_called()

    @patch("socialhome.content.signals.django_rq.enqueue")
    def test_local_content_with_parent_sent_as_reply(self, mock_send):
        user = UserFactory()
        parent = ContentFactory(author=user.profile)
        mock_send.reset_mock()
        content = ContentFactory(author=user.profile, parent=parent)
        self.assertTrue(content.local)
        call_args = [
            call(send_reply_notifications, content.id),
            call(send_reply, content.id),
        ]
        assert mock_send.call_args_list == call_args

    @patch("socialhome.content.signals.django_rq.enqueue")
    @patch("socialhome.content.signals.update_streams_with_content")
    def test_local_content_gets_sent(self, mock_update, mock_send):
        user = UserFactory()
        mock_send.reset_mock()
        content = ContentFactory(author=user.profile)
        self.assertTrue(content.local)
        mock_send.assert_called_once_with(send_content, content.id)

    @patch("socialhome.content.signals.django_rq.enqueue")
    @patch("socialhome.content.signals.update_streams_with_content")
    def test_share_gets_sent(self, mock_update, mock_send):
        user = UserFactory()
        user2 = UserFactory()
        share_of = ContentFactory(author=user2.profile)
        mock_send.reset_mock()
        content = ContentFactory(author=user.profile, share_of=share_of)
        call_args = [
            call(send_share_notification, content.id),
            call(send_share, content.id),
        ]
        assert mock_send.call_args_list == call_args


class TestFederateContentRetraction(SocialhomeTestCase):
    @patch("socialhome.content.signals.django_rq.enqueue")
    def test_non_local_content_retraction_does_not_get_sent(self, mock_send):
        content = ContentFactory()
        content.delete()
        mock_send.assert_not_called()

    @patch("socialhome.content.signals.django_rq.enqueue")
    def test_local_content_retraction_gets_sent(self, mock_send):
        user = UserFactory()
        content = ContentFactory(author=user.profile)
        self.assertTrue(content.local)
        mock_send.reset_mock()
        content_id = content.id
        content.delete()
        mock_send.assert_called_once_with(send_content_retraction, content, content.author_id)

    @patch("socialhome.content.signals.django_rq.enqueue", side_effect=Exception)
    @patch("socialhome.content.signals.logger.exception")
    def test_exception_calls_logger(self, mock_logger, mock_send):
        user = UserFactory()
        content = ContentFactory(author=user.profile)
        content.delete()
        self.assertTrue(mock_logger.called)


class TestFetchPreview(SocialhomeTestCase):
    @patch("socialhome.content.signals.fetch_content_preview")
    def test_fetch_content_preview_called(self, fetch):
        content = ContentFactory()
        fetch.assert_called_once_with(content)

    @patch("socialhome.content.signals.fetch_content_preview", side_effect=Exception)
    @patch("socialhome.content.signals.logger.exception")
    def test_fetch_content_preview_exception_logger_called(self, logger, fetch):
        ContentFactory()
        self.assertTrue(logger.called)


class TestRenderContent(SocialhomeTestCase):
    def test_render_content_called(self):
        content = ContentFactory()
        content.render = Mock()
        content.save()
        content.render.assert_called_once_with()

    @patch("socialhome.content.signals.logger.exception")
    def test_render_content_exception_logger_called(self, logger):
        content = ContentFactory()
        content.render = Mock(side_effect=Exception)
        content.save()
        self.assertTrue(logger.called)
