import {
  LayoutDashboard,
  MessageSquare,
  Users,
  BookOpen,
  Settings,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
}

export const navItems: NavItem[] = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/sessions", label: "Sessions", icon: MessageSquare },
  { href: "/preferences", label: "Preferences", icon: Users },
  { href: "/skills", label: "Skills", icon: BookOpen },
  { href: "/configure", label: "Configure", icon: Settings },
];
