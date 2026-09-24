import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp?: number;
  dispatches?: any[]; // For OrchestratorCanvas compatibility
}

interface ChatState {
  threads: Record<string, ChatMessage[]>;
  addMessage: (threadKey: string, message: ChatMessage) => void;
  setMessages: (threadKey: string, messages: ChatMessage[]) => void;
  clearThread: (threadKey: string) => void;
}

export const useChatStore = create<ChatState>()(persist((set) => ({
  threads: {},
  addMessage: (threadKey, message) => set((state) => ({
    threads: {
      ...state.threads,
      [threadKey]: [...(state.threads[threadKey] || []), message]
    }
  })),
  setMessages: (threadKey, messages) => set((state) => ({
    threads: {
      ...state.threads,
      [threadKey]: messages
    }
  })),
  clearThread: (threadKey) => set((state) => {
    const newThreads = { ...state.threads };
    delete newThreads[threadKey];
    return { threads: newThreads };
  })
}), { name: 'chat-storage' }));
