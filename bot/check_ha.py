import asyncio
from config import config
from ha_service import ha_service
from service_container import ServiceContainer

async def main():
    container = ServiceContainer(ha=ha_service)
    await container.ha.refresh_entities()
    states = await container.ha.get_live_states("device_tracker")
    for s in states:
        mac = s.get("attributes", {}).get("mac")
        if mac:
            print(f"{s.get('entity_id')} - {s.get('state')} - {mac}")
    print("Done")

if __name__ == "__main__":
    asyncio.run(main())
