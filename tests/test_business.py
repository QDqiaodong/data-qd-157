import unittest,json
from pathlib import Path
from license_planner.model import analyze
from license_planner.expression import parse,alternatives
ROOT=Path(__file__).resolve().parents[1]
def fixture():return json.loads((ROOT/'fixtures/input.json').read_text())
class Licensing(unittest.TestCase):
 def test_sample(self):
  r=analyze(fixture());self.assertFalse(r['compliant']);self.assertEqual(r['components'][0]['selected'],{'licenses':['MIT'],'obligations':['notice']})
 def test_precedence(self):self.assertEqual(alternatives(parse('A OR B AND C')),{frozenset(['A']),frozenset(['B','C'])})
 def test_parentheses(self):self.assertEqual(alternatives(parse('(A OR B) AND C')),{frozenset(['A','C']),frozenset(['B','C'])})
 def test_with_is_atomic(self):self.assertEqual(parse('GPL-2.0-only WITH Classpath-exception-2.0'),('license','GPL-2.0-only WITH Classpath-exception-2.0'))
 def test_policy_with_does_not_inherit(self):
  d=fixture();d['components']=[{'id':'x','expression':'MIT WITH Exception'}];self.assertEqual(analyze(d)['components'][0]['status'],'blocked')
 def test_invalid_syntax(self):
  for text in ['A AND','(A','A B','A WITH (B)','A || B','A OR OR B','(A) WITH E']:
   with self.subTest(text=text),self.assertRaises(ValueError):parse(text)
 def test_union_obligations(self):
  d=fixture();d['components']=[{'id':'x','expression':'MIT AND Apache-2.0'}];self.assertEqual(analyze(d)['components'][0]['selected']['obligations'],['notice','patent'])
 def test_duplicate_component(self):
  d=fixture();d['components'].append(d['components'][0])
  with self.assertRaises(ValueError):analyze(d)
 def test_search_limit(self):
  expression=' AND '.join(f'(A{i} OR B{i})' for i in range(9))
  with self.assertRaisesRegex(ValueError,'alternatives'):alternatives(parse(expression))
 def test_order_independent_selection(self):
  d=fixture();a=analyze(d);d['policy'].reverse();d['components'].reverse();self.assertEqual(a,analyze(d))
