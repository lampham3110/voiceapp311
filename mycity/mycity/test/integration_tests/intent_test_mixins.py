import unittest

class IntentRepromptTextTestMixIn:

    def test_returns_reprompt_text(self):
        response = self.controller.on_intent(self.request)
        if self.returns_reprompt_text:
            self.assertIsNotNone(response.reprompt_text)
        else:
            self.assertIsNone(response.reprompt_text)


class IntentCardTitleTestMixIn:

    def test_returns_correct_title_card(self):
        response = self.controller.on_intent(self.request)
        self.assertEqual(response.card_title, self.intent_to_test)


# there are some intents where it makes sense to write custom tests for error
# messages so we can abstract the most common test as a mix in

class IntentTestForErrorMixIn:

    def test_for_error_message(self):
        response = self.controller.on_intent(self.request)
        self.assertNotIn("Uh oh", response.output_speech)
        self.assertNotIn("Error", response.output_speech)
