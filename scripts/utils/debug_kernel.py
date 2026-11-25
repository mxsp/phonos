import sys
import os
import asyncio
import logging

# Setup path
sys.path.insert(0, os.getcwd())
sys.path.append(os.path.join(os.getcwd(), 'production'))

# Configure logging
logging.basicConfig(level=logging.INFO)

from phonos_app.helpers.kernel_manager import kernel_manager

async def test_start():
    print("Testing kernel start...")
    
    # Configure kernel manager manually (simulating api_notebooks.py)
    kernel_manager.config = {
        'NOTEBOOK_WORKSPACE_BASE': os.path.abspath('test_notebook_data'),
        'NOTEBOOK_MEM_LIMIT': '1g',
        'NOTEBOOK_CPU_QUOTA': 1.0
    }
    
    try:
        session = await kernel_manager.start_kernel(
            user_id=1, 
            notebook_id=999, 
            project_id=None
        )
        print(f"Session started successfully: {session.session_id}")
        
        # Stop it
        await kernel_manager.stop_kernel(session.session_id)
        print("Session stopped.")
        
    except Exception as e:
        print(f"FAILED: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_start())
