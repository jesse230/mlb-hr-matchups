import httpx
import asyncio

async def main():
    client = httpx.AsyncClient(base_url='https://statsapi.mlb.com/api/v1', timeout=15.0)
    
    r = await client.get('/schedule', params={
        'date': '2026-04-24',
        'sportId': 1,
        'hydrate': 'team,linescore,probablePitcher'
    })
    data = r.json()
    
    for day_data in data.get('dates', []):
        for game in day_data.get('games', []):
            away = game['teams']['away']['team']['name']
            home = game['teams']['home']['team']['name']
            hp = game['teams']['home'].get('probablePitcher')
            ap = game['teams']['away'].get('probablePitcher')
            hp_name = hp.get('fullName', 'TBD') if hp else 'TBD'
            ap_name = ap.get('fullName', 'TBD') if ap else 'TBD'
            hp_id = hp.get('id', 'N/A') if hp else 'N/A'
            ap_id = ap.get('id', 'N/A') if ap else 'N/A'
            print(f'{away} @ {home} | HP: {hp_name} (id={hp_id}) | AP: {ap_name} (id={ap_id})')
    
    await client.aclose()

asyncio.run(main())
