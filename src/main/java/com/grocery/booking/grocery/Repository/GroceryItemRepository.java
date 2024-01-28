package com.grocery.booking.grocery.Repository;

import com.grocery.booking.grocery.Entity.GroceryItem;
import org.springframework.data.jpa.repository.JpaRepository;

public interface GroceryItemRepository extends JpaRepository<GroceryItem, Long> {
}
