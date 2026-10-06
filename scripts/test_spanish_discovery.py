"""Spanish public-feed selection must retain relevant notices without inventing incidents."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import public_data_pipeline as pipeline

class SpanishDiscoveryTests(unittest.TestCase):
    def discover(self, title, tier='local-news'):
        source=dict(id='reviewed-source', language='es', countryCodes=['SV'], tier=tier)
        item=dict(title=title, description='', url='https://publisher.example/report',
                  publishedAt='Tue, 06 Oct 2026 01:00:00 GMT')
        return pipeline.news_discovery(source,[item],'2026-10-06T02:00:00Z',{'SV':{'code':'SV','name':'El Salvador'}})

    def test_official_color_warning_is_unknown_candidate_without_false_weather_category(self):
        for color in ('AMARILLA','ROJA','NARANJA','VERDE'):
            title='ALERTA '+color+' focalizada para una zona pendiente de revisión'
            with self.subTest(color=color):
                self.assertEqual(self.discover(title,'official')[0]['categoryCandidates'],['other'])
                self.assertEqual(self.discover(title),[])
        self.assertEqual(self.discover('Alerta sobre cambio de horarios','official'),[])
        self.assertEqual(self.discover('Alerta roja por tormentas','official')[0]['categoryCandidates'],['weather'])

    def test_road_accidents_are_retained_and_unrelated_accidents_are_excluded(self):
        for title in ('Accidente de tránsito deja lesionados','Dos siniestros viales reportados',
                      'VMT refuerza controles tras accidentes mortales de transporte pesado'):
            with self.subTest(title=title):
                self.assertEqual(self.discover(title)[0]['categoryCandidates'],['transport'])
        for title in ('Accidente laboral en una fábrica','Un accidente político inesperado',
                      'Transporte pesado anuncia nuevos horarios','Siniestro incendio industrial'):
            with self.subTest(title=title): self.assertEqual(self.discover(title),[])

    def test_weather_and_crime_remain_unverified_discoveries_with_complete_source_headlines(self):
        for title,category in [('Sequía prolongada','weather'),('Sequia prolongada','weather'),
                               ('Tormentas previstas','weather'),('Investigan un homicidio','crime'),
                               ('Denuncian secuestros y robos','crime')]:
            with self.subTest(title=title):
                row=self.discover(title)[0]
                self.assertIn(category,row['categoryCandidates'])
                self.assertEqual(row['title'],title)
                self.assertEqual(row['status'],'needs-review')
                self.assertEqual(row['locationStatus'],'unverified')
                for key in ('countryCode','location','occurredAt','severity'): self.assertNotIn(key,row)

if __name__=='__main__': unittest.main()
