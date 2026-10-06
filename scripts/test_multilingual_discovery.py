"""Discovery recall checks; matches must never become confirmed incidents."""
import unittest
from public_data_pipeline import news_discovery
SOURCE={'id':'reviewed-feed','language':'es','countryCodes':['CR']}
COUNTRIES={'CR':{'code':'CR','name':'Costa Rica'}}
class MultilingualDiscoveryTests(unittest.TestCase):
 def discover(self,title,description=''):
  return news_discovery(SOURCE,[{'title':title,'description':description,'url':'https://example.org/report','publishedAt':'2026-10-05T12:00:00Z','guid':'report'}],'2026-10-05T13:00:00Z',COUNTRIES)
 def test_transport_spanish_french(self):
  for title in ['Ruta 32 cerrada por caída de material','Cierre de la carretera entre dos comunidades','Puente colapsado deja comunidades aisladas','Cancelación de vuelos por tormenta','Deslizamiento impide el paso','Route coupée après les fortes pluies','Fermeture du pont après un accident','Les vols sont annulés','Déraillement près de la gare']:
   with self.subTest(title=title):
    row=self.discover(title)[0];self.assertIn('transport',row['categoryCandidates']);self.assertEqual(row['status'],'needs-review');self.assertEqual(row['locationStatus'],'unverified');self.assertEqual(row['mentionedCountryCodes'],[]);self.assertNotIn('countryCode',row);self.assertNotIn('occurredAt',row)
 def test_non_incident_uses_do_not_match(self):
  for title in ['Puente cultural entre pueblos','Ruta turística reúne a empresarios','Un vol de bijoux au musée','Nouveau train pour les touristes']:
   self.assertFalse(self.discover(title),title)
 def test_complete_input_and_metadata_only_output(self):
  description='Texto de contexto. '*12000+' Cierre de la ruta por inundaciones.'
  row=self.discover('Información regional',description)[0]
  self.assertIn('transport',row['categoryCandidates']);self.assertIn('flood',row['categoryCandidates']);self.assertNotIn('description',row);self.assertNotIn('excerpt',row)
 def test_reporting_correction_is_a_review_candidate(self):
  row=self.discover('Desmienten el cierre de la ruta anunciado ayer')[0]
  self.assertEqual(row['status'],'needs-review');self.assertNotIn('state',row)
if __name__=='__main__':unittest.main()
