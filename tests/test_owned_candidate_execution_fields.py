import unittest

from aos.owned_form_candidate_execution import candidate_operator_field_args
from aos.web_https_form_transport import exact_form_fields


class OwnedCandidateExecutionFieldTests(unittest.TestCase):
    def test_single_candidate_binding_uses_the_single_field_operator_mode(self):
        fields = (('message', 'candidate-value'),)
        field_name, value = candidate_operator_field_args(fields)

        self.assertEqual((field_name, value), ('message', 'candidate-value'))
        self.assertEqual(exact_form_fields(field_name, value), fields)

    def test_multiple_bindings_fail_closed_for_candidate_execution(self):
        fields = exact_form_fields(None, None, [
            {'name': 'message', 'value': 'candidate-value'},
            {'name': 'category', 'value': 'sample'},
        ])

        with self.assertRaisesRegex(ValueError, 'requires_one_parameter_binding'):
            candidate_operator_field_args(fields)


if __name__ == '__main__':
    unittest.main()
